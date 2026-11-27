"""Corner-matrix min/max extremes in the `klt sim` measurement rollup (#5221)."""

from klayout_tools import sim, sim_corner_extremes


def _corner(cid, value, margin=None, status="pass"):
    return {
        "corner_id": cid,
        "monte_carlo": None,
        "measurements": [
            {"name": "c", "value": value, "margin": margin, "status": status}
        ],
    }


def test_min_max_unlimited_middle_extreme():
    """min/max are true extremes; worst_case stays the first corner."""
    corners = [
        _corner("a", 21.0),
        _corner("b", 20.4),
        _corner("c", 22.8),
        _corner("d", 22.8),
    ]
    (entry,) = sim._rollup_measurements([{"name": "c", "unit": "fF"}], corners)

    assert entry["min"] == {"corner_id": "b", "value": 20.4}
    assert entry["max"] == {"corner_id": "c", "value": 22.8}  # tie -> first
    assert entry["worst_case"]["corner_id"] == "a"


def test_min_max_skip_absent_and_all_absent():
    corners = [_corner("a", None, status="absent"), _corner("b", 3.0)]
    (entry,) = sim._rollup_measurements([{"name": "c"}], corners)
    assert entry["min"] == entry["max"] == {"corner_id": "b", "value": 3.0}

    (entry,) = sim._rollup_measurements(
        [{"name": "c"}], [_corner("a", None, status="absent")]
    )
    assert entry["min"] is None and entry["max"] is None


def test_min_max_skip_bool_nan_and_non_numeric():
    corners = [
        _corner("a", True),
        _corner("b", float("nan")),
        _corner("c", "x"),
        _corner("d", 2.0),
    ]
    extremes = sim_corner_extremes.corner_extremes(
        sim_corner_extremes.corner_entries("c", corners)
    )
    assert extremes["min"] == extremes["max"] == {"corner_id": "d", "value": 2.0}


def test_min_max_with_limits_keeps_worst_case():
    corners = [_corner("a", 1.0, margin=0.5), _corner("b", 1.2, margin=0.1)]
    (entry,) = sim._rollup_measurements(
        [{"name": "c", "limits": {"max": 1.3}}], corners
    )
    assert entry["worst_case"]["corner_id"] == "b"
    assert entry["min"]["corner_id"] == "a"
    assert entry["max"]["corner_id"] == "b"


def test_min_max_skip_infinity():
    """+/-infinity is not a finite value, so it is never an extreme (#5580)."""
    corners = [
        _corner("a", float("-inf")),
        _corner("b", 1.0),
        _corner("c", float("inf")),
        _corner("d", 2.0),
    ]
    extremes = sim_corner_extremes.corner_extremes(
        sim_corner_extremes.corner_entries("c", corners)
    )
    assert extremes["min"] == {"corner_id": "b", "value": 1.0}
    assert extremes["max"] == {"corner_id": "d", "value": 2.0}


def test_min_max_all_infinite_is_none():
    """A measurement whose every corner is infinite has no extremes (#5580)."""
    corners = [_corner("a", float("inf")), _corner("b", float("-inf"))]
    extremes = sim_corner_extremes.corner_extremes(
        sim_corner_extremes.corner_entries("c", corners)
    )
    assert extremes["min"] is None and extremes["max"] is None
