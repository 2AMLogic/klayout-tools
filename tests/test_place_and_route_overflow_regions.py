"""Per-region global-route overflow (issues #5313, #6949).

``gcd_global_route_congestion.rpt`` is a real ``global_route
-congestion_report_file`` capture (OpenROAD 26Q3-1510-g6cb3f2b704, ``gcd``
placement with ``set_global_routing_layer_adjustment met1-met5 0.9`` to force
overflow). It carries no macros, so attribution is tested on synthetic boxes
over the real GCell coordinates; no live macro run is claimed here.
"""

import os
import shutil

from klayout_tools import place_and_route_reports as reports
from klayout_tools.place_and_route_route_overflow import grt_overflow

FIXTURE = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "place_and_route",
    "gcd_global_route_congestion.rpt",
)
_ZERO = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
_ZERO_GROUP = {
    "overflow_sum": 0,
    "violation_tile_count": 0,
    "zero_capacity_tile_count": 0,
    "zero_capacity_overflow_sum": 0,
}


def _congestion_record(direction: str, cap: int, use: int, cong: int, bbox) -> str:
    x1, y1, x2, y2 = bbox
    return (
        f"violation type: {direction} congestion\n"
        "    srcs: net:_a_ net:_b_\n"
        f"    comment: capacity:{cap} usage:{use} congestion:{cong}\n"
        f"    bbox = ({x1}, {y1}) - ({x2}, {y2}) on Layer -\n"
    )


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


def test_grt_overflow_digest_aggregates_full_report_before_truncation(tmp_path):
    """Issue #6949: one read of the report yields both the capped worst-20
    list and the per-region aggregates over *every* record -- the capture's
    worst-20 sample sums to 128, its full population to 847, so the sample
    cannot stand in for the totals."""
    shutil.copy(FIXTURE, tmp_path / "gcd_route_congestion.rpt")
    digest = grt_overflow(str(tmp_path), "gcd", [])
    assert digest is not None
    regions = digest["overflow_regions"]
    assert len(regions) == reports.OVERFLOW_REGIONS_TOP_N
    assert sum(r["overflow"] for r in regions) == 128
    # Contents, order (ties keep report order) and cap are unchanged.
    assert regions == reports.parse_congestion_report(FIXTURE, [])
    by_region = digest["overflow_by_region"]
    assert by_region["horizontal"]["elsewhere"] == {
        "overflow_sum": 380,
        "violation_tile_count": 107,
        "zero_capacity_tile_count": 0,
        "zero_capacity_overflow_sum": 0,
    }
    assert by_region["vertical"]["elsewhere"] == {
        "overflow_sum": 467,
        "violation_tile_count": 110,
        "zero_capacity_tile_count": 0,
        "zero_capacity_overflow_sum": 0,
    }
    # No macros in this run: the attributed groups read as all-zero counters,
    # never as missing keys.
    for direction in ("horizontal", "vertical"):
        for region in ("inside_footprint", "keepout_band"):
            assert by_region[direction][region] == _ZERO_GROUP
    total = sum(
        group["overflow_sum"]
        for per_direction in by_region.values()
        for group in per_direction.values()
    )
    assert total == 847


def test_grt_overflow_synthetic_attribution_and_counters(tmp_path):
    """Issue #6949: exact counters over a synthetic report -- a GCell whose
    centre sits inside u_a's footprint *and* u_b's keepout band (footprint
    precedence), a band-only hit on u_c, an outside hit, zero-capacity
    tiles, and both directions sharing one bounding box."""
    macros = [
        {
            "instance": "u_a",
            "x_um": 5.0,
            "y_um": 5.0,
            "width_um": 20.0,
            "height_um": 20.0,
            "orientation": "R0",
            "halo": None,
            "keepout": None,
        },
        {
            "instance": "u_b",
            "x_um": 25.0,
            "y_um": 5.0,
            "width_um": 20.0,
            "height_um": 20.0,
            "orientation": "R0",
            "halo": None,
            "keepout": {"left": 10.0},
        },
        {
            "instance": "u_c",
            "x_um": 60.0,
            "y_um": 60.0,
            "width_um": 20.0,
            "height_um": 20.0,
            "orientation": "R0",
            "halo": {"left": 20.0},
            "keepout": None,
        },
    ]
    report = "".join(
        [
            # Centre (15,15): inside u_a's footprint and inside u_b's band.
            _congestion_record("Horizontal", 0, 3, 3, (10.0, 10.0, 20.0, 20.0)),
            # Same bounding box, other direction: each counts exactly once.
            _congestion_record("Vertical", 0, 2, 2, (10.0, 10.0, 20.0, 20.0)),
            # Centre (45,70): u_c's halo band only.
            _congestion_record("Horizontal", 2, 5, 3, (40.0, 65.0, 50.0, 75.0)),
            # Centre (200,200): no macro at all.
            _congestion_record("Vertical", 4, 9, 5, (195.0, 195.0, 205.0, 205.0)),
            # Centre (200,10): no macro at all.
            _congestion_record("Horizontal", 1, 4, 3, (195.0, 5.0, 205.0, 15.0)),
        ]
    )
    (tmp_path / "top_route_congestion.rpt").write_text(report)
    digest = grt_overflow(str(tmp_path), "top", macros)
    assert digest is not None
    assert digest["overflow_by_region"] == {
        "horizontal": {
            "inside_footprint": {
                "overflow_sum": 3,
                "violation_tile_count": 1,
                "zero_capacity_tile_count": 1,
                "zero_capacity_overflow_sum": 3,
            },
            "keepout_band": {
                "overflow_sum": 3,
                "violation_tile_count": 1,
                "zero_capacity_tile_count": 0,
                "zero_capacity_overflow_sum": 0,
            },
            "elsewhere": {
                "overflow_sum": 3,
                "violation_tile_count": 1,
                "zero_capacity_tile_count": 0,
                "zero_capacity_overflow_sum": 0,
            },
        },
        "vertical": {
            "inside_footprint": {
                "overflow_sum": 2,
                "violation_tile_count": 1,
                "zero_capacity_tile_count": 1,
                "zero_capacity_overflow_sum": 2,
            },
            "keepout_band": _ZERO_GROUP,
            "elsewhere": {
                "overflow_sum": 5,
                "violation_tile_count": 1,
                "zero_capacity_tile_count": 0,
                "zero_capacity_overflow_sum": 0,
            },
        },
    }
    ranked = [
        (r["direction"], r["overflow"], r["macro"], r["where"])
        for r in digest["overflow_regions"]
    ]
    assert ranked == [
        ("vertical", 5, None, None),
        ("horizontal", 3, "u_a", "inside_footprint"),
        ("horizontal", 3, "u_c", "keepout_band"),
        ("horizontal", 3, None, None),
        ("vertical", 2, "u_a", "inside_footprint"),
    ]


def test_grt_overflow_without_report_is_none(tmp_path):
    assert grt_overflow(str(tmp_path), "gcd", []) is None
    (tmp_path / "gcd_route_congestion.rpt").write_text("")
    assert grt_overflow(str(tmp_path), "gcd", []) is None


def test_build_congestion_null_reason_and_value():
    base = dict(global_route=None, completeness=None, placement={}, detailed={})
    c = reports.build_congestion(**base)
    assert c["global_route"]["overflow_regions"] is None
    assert c["global_route"]["overflow_by_region"] is None
    assert "global_route.overflow_regions" in c["null_reasons"]
    assert "global_route.overflow_by_region" in c["null_reasons"]
    digest = {
        "overflow_regions": [{"overflow": 1}],
        "overflow_by_region": {"horizontal": {}},
    }
    c = reports.build_congestion(**base, overflow=digest)
    assert c["global_route"]["overflow_regions"] == digest["overflow_regions"]
    assert c["global_route"]["overflow_by_region"] == digest["overflow_by_region"]
    assert "global_route.overflow_regions" not in c["null_reasons"]
    assert "global_route.overflow_by_region" not in c["null_reasons"]
