"""Direct checks for declared and measured placement rectangles."""

import pytest

from klayout_tools.gen_compose import check_placement_overlap


def test_check_placement_overlap_reports_positive_area_intersection():
    # Acceptance criterion: A at (0, 0)-(10, 10) and B at (9, 2)-(12, 5)
    # report the intersection (9, 2)-(10, 5), in um.
    failures = check_placement_overlap(
        {
            "A": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "B": {"x0": 9.0, "y0": 2.0, "x1": 12.0, "y1": 5.0},
        }
    )
    assert len(failures) == 1
    assert "'A'" in failures[0]
    assert "'B'" in failures[0]
    assert "(9.000, 2.000)-(10.000, 5.000)" in failures[0]


def test_check_placement_overlap_edge_corner_and_clear_pass():
    base = {"A": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0}}
    # Shared edge: zero-width intersection is not a positive-area overlap.
    assert (
        check_placement_overlap(
            {**base, "B": {"x0": 10.0, "y0": 2.0, "x1": 12.0, "y1": 5.0}}
        )
        == []
    )
    # Shared corner: zero width *and* height.
    assert (
        check_placement_overlap(
            {**base, "B": {"x0": 10.0, "y0": 10.0, "x1": 12.0, "y1": 12.0}}
        )
        == []
    )
    # Clear of A entirely.
    assert (
        check_placement_overlap(
            {**base, "B": {"x0": 11.0, "y0": 2.0, "x1": 12.0, "y1": 5.0}}
        )
        == []
    )


def test_check_placement_overlap_contained_box_fails():
    # A box fully inside another still intersects it with positive width and
    # height -- a positive-area overlap is a failure however nested.
    failures = check_placement_overlap(
        {
            "A": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "B": {"x0": 2.0, "y0": 2.0, "x1": 4.0, "y1": 4.0},
        }
    )
    assert len(failures) == 1
    assert "(2.000, 2.000)-(4.000, 4.000)" in failures[0]


def test_check_placement_overlap_intentional_edge_abutment_passes():
    # Abutting by design: two boxes touching along a full edge pass the gate
    # (only a *positive-area* intersection fails -- the compose-level `abuts`
    # exemption question is separate and covered below).
    assert (
        check_placement_overlap(
            {
                "left": {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0},
                "right": {"x0": 5.0, "y0": 0.0, "x1": 9.0, "y1": 5.0},
            }
        )
        == []
    )


def test_check_placement_overlap_isolated_growth_fails_with_side_deltas():
    # A measured footprint extending past its own declaration fails even
    # with no second block anywhere: the block ID, both rectangles, and
    # which sides grew by how much are all reported.
    failures = check_placement_overlap(
        {"macro": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 2.0}},
        {"macro": {"x0": -1.0, "y0": 0.0, "x1": 2.0, "y1": 3.0}},
    )
    assert len(failures) == 1
    failure = failures[0]
    assert "macro" in failure
    assert "(-1.000, 0.000)-(2.000, 3.000)" in failure  # measured
    assert "(0.000, 0.000)-(2.000, 2.000)" in failure  # declared
    assert "grew" in failure
    assert "left 1.000um" in failure
    assert "top 1.000um" in failure


def test_check_placement_overlap_declaration_enclosing_geometry_passes():
    # A declaration that encloses the geometry passes the growth check.
    assert (
        check_placement_overlap(
            {"a": {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0}},
            {"a": {"x0": 0.5, "y0": 0.5, "x1": 4.5, "y1": 4.5}},
        )
        == []
    )


def test_check_placement_overlap_pair_failure_includes_participant_deltas():
    # On a pair failure, a participant whose measured footprint differs
    # from its declaration gets its growth deltas folded into the pair
    # report (plus its own standalone growth failure).
    failures = check_placement_overlap(
        {
            "A": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
            "B": {"x0": 9.0, "y0": 2.0, "x1": 12.0, "y1": 5.0},
        },
        {"A": {"x0": -1.0, "y0": 0.0, "x1": 10.0, "y1": 10.0}},
    )
    assert len(failures) == 2
    pair_failure = failures[0]
    assert "'A'" in pair_failure
    assert "'B'" in pair_failure
    assert "(9.000, 2.000)-(10.000, 5.000)" in pair_failure
    assert "left 1.000um" in pair_failure  # A's footprint delta, in context
    growth_failure = failures[1]
    assert "grew" in growth_failure
    assert "macro" not in growth_failure
    assert "'A'" in growth_failure


def test_check_placement_overlap_rejects_malformed_input():
    with pytest.raises(ValueError):
        check_placement_overlap(
            {"A": {"x0": 0.0, "y0": 0.0, "x1": 2.0}}  # missing y1
        )
    with pytest.raises(ValueError):
        check_placement_overlap({"A": "not a box"})
    with pytest.raises(ValueError):
        check_placement_overlap(
            {"A": {"x0": 2.0, "y0": 0.0, "x1": 0.0, "y1": 2.0}}  # x1 < x0
        )
    with pytest.raises(ValueError):
        check_placement_overlap(
            {"A": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 2.0}},
            {"unknown": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}},
        )


@pytest.mark.parametrize("width", [0.0002, 0.0001, 0.00005])
def test_positive_area_on_subgrid_coordinates_is_visible(width):
    a = {"x0": -1, "y0": -1, "x1": 0, "y1": 1}
    b = {"x0": -width, "y0": -1, "x1": 1, "y1": 1}
    (failure,) = check_placement_overlap({"a": a, "b": b})
    assert f"({-width!r}, -1.000)-(0.000, 1.000)" in failure


def test_pair_diagnostics_include_inward_and_outward_deltas():
    a = {"x0": -1, "y0": -1, "x1": 0, "y1": 1}
    b = {"x0": -0.5, "y0": -1, "x1": 1, "y1": 1}
    measured = {"a": dict(a, x1=-0.25), "b": dict(b, y0=-1.25)}
    pair, growth = check_placement_overlap({"a": a, "b": b}, measured)
    assert "'a' measured" in pair and "right -0.250um" in pair
    assert "'b' measured" in pair and "bottom 0.250um" in pair
    assert "block 'b'" in growth
    assert check_placement_overlap({"a": a}, {"a": measured["a"]}) == []


@pytest.mark.parametrize("bad_x", [True, float("nan"), float("inf")])
def test_gate_rejects_non_numeric_or_nonfinite_coordinates(bad_x):
    with pytest.raises(ValueError, match="finite number"):
        check_placement_overlap({"a": {"x0": bad_x, "y0": 0, "x1": 1, "y1": 1}})


def test_gate_rejects_empty_block_id():
    with pytest.raises(ValueError, match="non-empty strings"):
        check_placement_overlap({"": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}})
