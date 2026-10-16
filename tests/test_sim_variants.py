"""Issue #3252: ``corners.variants`` -- named, correlated source arrangements
crossed with the process x supply_v x temperature axes."""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim


def _variants():
    return [
        {"name": "low", "sources": {"va": 1.0, "vb": "va"}},
        {"name": "high", "sources": {"va": 1.8, "vb": 3.3}},
    ]


def test_no_variants_is_unchanged():
    points = sim._expand_corners({"process": ["tt"], "temperature_c": [27]}, [])
    assert [p.corner_id for p in points] == ["tt/novdd/27C"]
    assert points[0].variant is None


def test_variants_cross_axes_with_unique_ids():
    points = sim._expand_corners(
        {"process": ["tt", "ss"], "variants": _variants(), "temperature_c": [27, 85]},
        [],
    )
    ids = [p.corner_id for p in points]
    assert len(points) == 8 and len(set(ids)) == 8
    assert len({p.slug for p in points}) == 8
    assert ids[0] == "tt/va=1.000_vb=1.000V/27C/@low"
    assert points[0].supply_v == {"va": 1.0, "vb": 1.0}
    assert points[2].supply_v == {"va": 1.8, "vb": 3.3}


def test_expression_resolution_order_independent_and_arithmetic():
    resolved = sim._parse_variants(
        [{"name": "x", "sources": {"b": "a*0.5 + (a - 0.2)", "a": 2}}]
    )
    assert resolved == [("x", {"b": pytest.approx(1.0 + 1.8), "a": 2.0})]


@pytest.mark.parametrize(
    "sources, fragment",
    [
        ({"a": "b", "b": "a"}, "cyclic"),
        ({"a": "nope + 1"}, "unknown source name 'nope'"),
        ({"a": "1 +"}, "unexpected end"),
        ({"a": "__import__('os')"}, "cannot parse"),
        ({"a": True}, "number or an arithmetic"),
        ({"a": 1, "b": "a/0"}, "division by zero"),
    ],
)
def test_bad_expressions_rejected_naming_offender(sources, fragment):
    with pytest.raises(sim.SimError) as err:
        sim._parse_variants([{"name": "v", "sources": sources}])
    assert fragment in str(err.value)
    assert "'v'" in str(err.value)


def test_slug_collisions_rejected():
    with pytest.raises(sim.SimError, match="artifact slug"):
        sim._parse_variants(
            [{"name": "a.b", "sources": {"a": 1}}, {"name": "apb", "sources": {"a": 2}}]
        )
    with pytest.raises(sim.SimError, match="artifact slug"):
        sim._parse_variants(
            [{"name": "a-b", "sources": {"a": 1}}, {"name": "anb", "sources": {"a": 2}}]
        )


def test_variants_cannot_combine_with_units():
    spec = {"units": [{"temperature_c": 27}], "variants": _variants()}
    with pytest.raises(
        sim.SimError, match="corners.units cannot be combined with corners.variants"
    ):
        sim._expand_corners(spec, [])


def test_duplicate_and_empty_rejected():
    with pytest.raises(sim.SimError, match="duplicate name 'v'"):
        sim._parse_variants(
            [{"name": "v", "sources": {"a": 1}}, {"name": "v", "sources": {"a": 2}}]
        )
    with pytest.raises(sim.SimError, match="non-empty array"):
        sim._parse_variants([])
    with pytest.raises(sim.SimError, match="sources must be"):
        sim._parse_variants([{"name": "v", "sources": {}}])
    with pytest.raises(sim.SimError, match="name must be"):
        sim._parse_variants([{"name": "a/b", "sources": {"a": 1}}])


def test_variant_overrides_matching_supply_v_key():
    points = sim._expand_corners(
        {
            "supply_v": {"va": [1.2, 1.5], "vc": [5.0, 5.0]},
            "variants": [{"name": "t", "sources": {"va": 0.9}}],
        },
        [],
    )
    assert [p.supply_v for p in points] == [
        {"va": 0.9, "vc": 5.0},
        {"va": 0.9, "vc": 5.0},
    ]


def test_exclude_matches_on_variant():
    points = sim._expand_corners({"variants": _variants()}, [{"variant": "high"}])
    assert [p.variant for p in points] == ["low"]


def test_wire_round_trip_with_and_without_variant():
    points = sim._expand_corners({"variants": _variants()}, [])
    wire = json.loads(json.dumps(sim._corner_points_to_wire(points)))
    restored = sim._corner_points_from_wire(wire)
    assert [p.corner_id for p in restored] == [p.corner_id for p in points]
    assert [p.supply_v for p in restored] == [p.supply_v for p in points]

    for entry in wire:  # an older payload has no `variant` key at all
        entry.pop("variant")
    legacy = sim._corner_points_from_wire(wire)
    assert [p.variant for p in legacy] == [None, None]


def test_alter_lines_are_per_variant():
    points = sim._expand_corners({"variants": _variants()}, [])
    kwargs = dict(
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
        raw_path=None,
        osdi_preload=(),
    )
    low = sim._corner_deck_control_lines(point=points[0], **kwargs)
    high = sim._corner_deck_control_lines(point=points[1], **kwargs)
    assert "alter vb=1.0" in low and "alter vb=3.3" in high


def test_monte_carlo_samples_keep_variant_ids_distinct():
    points = sim._expand_corners({"variants": _variants()}, [])
    sampled, _ = sim._expand_monte_carlo(
        points, {"n": 2, "seed": 1, "vary": "mismatch"}
    )
    ids = [p.corner_id for p in sampled]
    assert len(set(ids)) == 4 and ids[0].endswith("/@low/mc0")


def test_xyce_refuses_variants():
    with pytest.raises(sim.SimError, match="alter"):
        sim._enforce_xyce_support_boundary(
            engine="xyce",
            backend="local",
            corners_spec={"variants": _variants()},
            monte_carlo_declared=False,
            fail_fast_probe=False,
        )
