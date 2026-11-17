"""Lossless supply ``corner_id`` spelling (issue #5870).

The legacy ``{value:.3f}`` supply label is not injective: two crossed
``corners.supply_v`` points whose rail values differ only below three
decimals resolved to one ``corner_id``, hence one artifact directory and one
``--resume`` key, and one simulation was mis-attributed to every colliding
point -- silently. The rule now spells ``.3f`` when it round-trips and falls
back to the shortest round-trip decimal (the #3035 ``corners.params`` rule)
otherwise; the crossed expansion refuses two points that still resolve to one
ID, mirroring ``corners.units``; and the off-host echo check covers a
supply-only grid so a legacy-``.3f`` runner image is refused instead of
trusted.

Unit tests need no simulator.
"""

from __future__ import annotations

import sys

import pytest

from klayout_tools import sim
from klayout_tools.sim_corner_point import (
    _format_supply_value,
    _supply_value_needs_full_precision,
)


def _expand(corners: dict, exclude: list | None = None) -> list[sim.CornerPoint]:
    return sim._expand_corners(corners, exclude or [])


def _patch_lossy_formatter(monkeypatch) -> None:
    """Restore the pre-#5870 formatter, so a test can construct an identity
    collision directly instead of relying on the shipped rule."""
    scp = sys.modules["klayout_tools.sim_corner_point"]
    monkeypatch.setattr(
        scp, "_format_supply_value", lambda value: f"{float(value):.3f}"
    )


#: The reporter's grid (issue #5870, "Observed concretely"): four points
#: differing only in a phase axis (ns offsets) and two differing only in a
#: window axis (ps/fs), every swept value far below the legacy spelling's
#: resolution. Six declared points, one legacy identity.
_REPORTER_SUPPLY = {
    "vsupply": [3.3] * 6,
    "vrel": [0.0] * 6,
    "vph": [0.0, 1.5e-9, 3e-9, 4.5e-9, 0.0, 0.0],
    "vthw": [0.0, 0.0, 0.0, 0.0, 5e-7, 2e-11],
}


# --- the spelling rule -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        # Every three-decimal supply label asserted anywhere in tests/
        # (measured 2026-11-15: 1.800V, 1.620V, 1.980V, 1.000V, 1.200V,
        # 3.300V, 1.700V) comes from a value exactly representable at three
        # decimals -- each keeps its byte-identical legacy label.
        (1.8, "1.800"),
        (1.62, "1.620"),
        (1.98, "1.980"),
        (1.0, "1.000"),
        (1.2, "1.200"),
        (3.3, "3.300"),
        (1.7, "1.700"),
        # Integral values round-trip and keep the padded legacy form.
        (5.0, "5.000"),
        (0.0, "0.000"),
        # Negative zero round-trips ("-0.000" == -0.0); pre-existing
        # rendering, left alone per the issue's out-of-scope list.
        (-0.0, "-0.000"),
        # A value finer than three decimals but exactly spelled by .3f.
        (1.234, "1.234"),
        # Values .3f would round away fall back to the shortest round-trip
        # decimal -- including 0.0005, which .3f rounded UP to 0.001.
        (0.0005, "0.0005"),
        (0.1234, "0.1234"),
        (1.5e-9, "1.5e-09"),
        (3e-9, "3e-09"),
        (4.5e-9, "4.5e-09"),
        (5e-7, "5e-07"),
        (2e-11, "2e-11"),
    ],
)
def test_supply_value_spelling_round_trips(value, text):
    assert _format_supply_value(value) == text
    assert float(text) == float(value)


@pytest.mark.parametrize(
    ("value", "needs"),
    [
        (3.3, False),
        (0.0, False),
        (-0.0, False),
        (1.234, False),
        (0.0005, True),
        (1.5e-9, True),
        (2e-11, True),
    ],
)
def test_supply_value_needs_full_precision(value, needs):
    assert _supply_value_needs_full_precision(value) is needs


def test_single_rail_label_keeps_legacy_text():
    (point,) = _expand({"process": ["ss"], "supply_v": {"vdd": [1.62]}})
    assert point.corner_id == "ss/1.620V/27C"
    assert point.slug == "ss_1p620V_27C"


def test_multi_rail_join_shape_unchanged():
    point = sim.CornerPoint("tt", {"vdd": 1.8, "vdda": 1.7}, 27)
    assert point.corner_id == "tt/vdd=1.800_vdda=1.700V/27C"


def test_sub_milli_rails_are_spelled_at_full_precision():
    assert sim.CornerPoint("tt", {"vph": 1.5e-9}, 27).corner_id == "tt/1.5e-09V/27C"
    point = sim.CornerPoint("tt", {"vph": 1.5e-9, "vthw": 5e-7}, 27)
    assert point.corner_id == "tt/vph=1.5e-09_vthw=5e-07V/27C"
    assert "/" not in point.slug
    assert point.slug != sim.CornerPoint("tt", {"vph": 3e-9, "vthw": 5e-7}, 27).slug


def test_units_form_sub_milli_values_get_distinct_ids():
    # The lossless spelling applies to ``corners.units`` too -- these two
    # units were refused as duplicate corner_ids under the legacy .3f
    # formatting (both 0.100) and are now two distinct points.
    points = _expand(
        {"units": [{"supply_v": {"vdd": 0.1004}}, {"supply_v": {"vdd": 0.1001}}]}
    )
    assert [p.corner_id for p in points] == [
        "default/0.1004V/27C",
        "default/0.1001V/27C",
    ]


def test_reporter_grid_first_point_keeps_legacy_label():
    # All of the first point's values are exactly representable at three
    # decimals, so its id is byte-identical to the legacy one the reporter
    # saw five times.
    (first, *rest) = _expand({"supply_v": _REPORTER_SUPPLY})
    assert (
        first.corner_id == "default/vph=0.000_vrel=0.000_vsupply=3.300_vthw=0.000V/27C"
    )
    assert len({p.corner_id for p in rest}) == 5
    assert first.corner_id not in {p.corner_id for p in rest}


# --- expansion: injectivity -------------------------------------------------


def test_crossed_sub_milli_points_get_distinct_ids_and_slugs():
    points = _expand({"supply_v": {"vph": [0.0, 1.5e-9]}})
    assert len(points) == 2
    assert len({p.corner_id for p in points}) == 2
    assert len({p.slug for p in points}) == 2


def test_reporter_six_point_grid_expands_to_six_distinct_ids_and_slugs():
    points = _expand({"supply_v": _REPORTER_SUPPLY})
    assert len(points) == 6
    ids = {p.corner_id for p in points}
    slugs = {p.slug for p in points}
    assert len(ids) == 6
    assert len(slugs) == 6


def test_legitimate_grids_still_expand():
    points = _expand(
        {
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.8, 1.62]},
            "temperature_c": [27, 125],
        }
    )
    assert len(points) == 8
    assert len({p.corner_id for p in points}) == 8


def test_sub_milli_supply_with_params_axis_keeps_distinct_hash_slugs():
    # On a params grid the slug takes the hash branch; distinct ids must
    # still give distinct directories.
    points = _expand({"supply_v": {"vph": [0.0, 1.5e-9]}, "params": {"r": [1000]}})
    assert len({p.corner_id for p in points}) == 2
    assert len({p.slug for p in points}) == 2


# --- expansion: the duplicate guard -----------------------------------------


def test_expansion_refuses_two_points_resolving_to_one_id(monkeypatch):
    """With the lossy pre-#5870 formatter the crossed expansion must refuse
    the grid, naming both colliding points, instead of silently merging them
    -- the collision is constructed directly, not via the shipped rule."""
    _patch_lossy_formatter(monkeypatch)
    with pytest.raises(sim.SimError) as raised:
        _expand({"supply_v": {"vph": [0.0, 1.5e-9]}})
    message = str(raised.value)
    assert "duplicates point 0" in message
    assert "default/0.000V/27C" in message


def test_expansion_guard_fires_on_the_reporter_grid(monkeypatch):
    _patch_lossy_formatter(monkeypatch)
    with pytest.raises(sim.SimError, match="point 1 duplicates point 0"):
        _expand({"supply_v": _REPORTER_SUPPLY})


def test_excluded_points_never_trip_the_duplicate_guard(monkeypatch):
    # The two colliding points are excluded before the guard sees them, so
    # an exclude rule keeps working under the lossy formatter too.
    _patch_lossy_formatter(monkeypatch)
    points = _expand(
        {"supply_v": {"vph": [0.0, 1.5e-9]}},
        exclude=[{"supply_v": {"vph": 1.5e-9}}],
    )
    assert len(points) == 1


# --- off-host echo check -----------------------------------------------------


def _corners_with_ids(ids):
    return [
        {"corner_id": corner_id, "status": "pass", "diagnostics": []}
        for corner_id in ids
    ]


def test_echo_accepts_honest_updated_worker():
    points = _expand({"supply_v": {"vph": [0.0, 1.5e-9]}})
    honest = _corners_with_ids([p.corner_id for p in points])
    for backend in ("remote", "batch"):
        sim._require_supply_echo(points, honest, backend=backend)


def test_echo_refuses_legacy_three_decimal_ids(monkeypatch):
    points = _expand({"supply_v": {"vph": [0.0, 1.5e-9]}})
    # What a pre-#5870 runner computes for the same coordinates: the id read
    # off each expanded point while the lossy formatter is in place. (The
    # expansion itself would now refuse the colliding grid -- that is the
    # guard above -- so the legacy response is synthesized per point.)
    _patch_lossy_formatter(monkeypatch)
    legacy = _corners_with_ids([p.corner_id for p in points])
    monkeypatch.undo()
    for backend in ("remote", "batch"):
        with pytest.raises(sim.SimError) as raised:
            sim._require_supply_echo(points, legacy, backend=backend)
        assert raised.value.code == sim.SUPPLY_PRECISION_UNSUPPORTED_WORKER_CODE
        message = str(raised.value)
        assert "1.5e-09" in message  # dispatched, full precision
        assert "0.000" in message  # came back, legacy three decimals
        assert "corners.supply_v" in message


def test_echo_refuses_collapsed_rows():
    points = _expand({"supply_v": {"vph": [0.0, 1.5e-9]}})
    honest_first = _corners_with_ids([points[0].corner_id])
    with pytest.raises(sim.SimError, match="1 corner report"):
        sim._require_supply_echo(points, honest_first, backend="batch")


def test_three_decimal_grids_are_never_gated():
    # A legacy image and an updated one spell identical ids here, so the
    # echo check must not fire -- even on ids that do not match at all.
    points = _expand({"supply_v": {"vdd": [1.8, 1.62]}})
    wrong = _corners_with_ids(["some/other/27C"] * len(points))
    for backend in ("remote", "batch"):
        sim._require_supply_echo(points, wrong, backend=backend)


def test_local_backend_is_never_gated():
    points = _expand({"supply_v": {"vph": [0.0, 1.5e-9]}})
    wrong = _corners_with_ids(["some/other/27C"] * len(points))
    sim._require_supply_echo(points, wrong, backend="local")
    sim._require_supply_echo(points, wrong, backend="local-parallel")


def test_supply_echo_gates_only_on_supply_values():
    # A params-only grid needs no supply gate: _require_params_echo owns it.
    points = _expand({"params": {"r": [1.0, 1.0000000000000002]}})
    wrong = _corners_with_ids(["some/other/27C"] * len(points))
    for backend in ("remote", "batch"):
        sim._require_supply_echo(points, wrong, backend=backend)


# --- runner-version registry --------------------------------------------------


def test_registry_entry_is_present_and_untagged():
    from klayout_tools import sim_batch_contract

    assert (
        sim_batch_contract.REQUEST_FEATURE_MIN_RUNNER_VERSION[
            "lossless supply_v corner_id spelling"
        ]
        is None
    )
