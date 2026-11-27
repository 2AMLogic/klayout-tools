"""Scaling guards for the ``derive`` reducer (:mod:`sim_derive_rollup`).

A group's completeness check is linear in the group, not O(expected x
reported): the observed positions are collected once, not once per expected
value. The counts are deterministic -- a ``points`` list that counts its own
walks -- so a regression to the per-expected-value rescan fails here rather
than only in a wall-clock budget (issue #7993).
"""

from __future__ import annotations

from klayout_tools import sim_derive_rollup
from test_sim_derive import GROUPS, derive, entry, grid, on_grid


class _CountingList(list):
    """A ``points`` list counting full walks: how many times the reducer
    scanned a group end to end."""

    def __init__(self, points: list) -> None:
        super().__init__(points)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _passing_group(size: int) -> _CountingList:
    """``size`` passing ``vout`` measurements, one at each integer code."""
    return _CountingList(
        [
            (
                {
                    "status": "pass",
                    "measurements": [
                        {"name": "vout", "value": float(code), "status": "pass"}
                    ],
                },
                code,
            )
            for code in range(size)
        ]
    )


def test_a_group_is_walked_a_fixed_number_of_times_however_many_its_values():
    small, large = _passing_group(3), _passing_group(1_024)
    for points in (small, large):
        codes = list(range(len(points)))
        group = sim_derive_rollup._reduce_group(derive(), {}, points, codes)
        assert (group["status"], group["value"]) == ("pass", float(codes[-1]))
    assert small.iterations == large.iterations == 3


def test_missing_values_are_named_in_expected_order_after_a_single_walk():
    points = _passing_group(1_024)
    group = sim_derive_rollup._reduce_group(
        derive(), {}, points, [1_025, 1_024, *range(1_024)]
    )
    assert (group["reason"], group["missing_axis_values"]) == (
        "missing_axis_points",
        [1_025, 1_024],
    )
    assert points.iterations == 1


def test_another_groups_observations_do_not_fill_a_groups_gap():
    corners = [
        c for c in grid(GROUPS) if (c["process"], c["params"]["code"]) != ("b", 128)
    ]
    rollup = entry(on_grid(derive(group_by=["process", "supply_v"])), corners)
    complete, missing = rollup["derive"]["groups"]
    assert (complete["group_id"], complete["status"], complete["value"]) == (
        "process=a/supply_v=1.000V",
        "pass",
        1.15 - 0.80,
    )
    assert (missing["group_id"], missing["reason"], missing["missing_axis_values"]) == (
        "process=b/supply_v=2.000V",
        "missing_axis_points",
        [128],
    )
