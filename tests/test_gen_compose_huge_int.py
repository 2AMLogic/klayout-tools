"""Huge-JSON-int robustness regressions for ``gen_compose`` (issue #6346).

The ``is_number(x)`` -> ``float(x)`` validate-then-convert pairs in
``gen_compose.py`` that PR #6341 (issue #6233) did not cover -- filed
separately because #6233's discovery grep only matched the positive
``if is_number(`` form. Curated set (verified on main): the four filed
sites -- ``_parse_cell_ports``, ``_parse_array_placement``'s
``_positive_number``, ``_parse_placement``, ``_port_probe_position_um`` --
plus ``parse_waypoints_um`` (validate at one line, convert further down),
and the drc-hints/port-width reads #6341 carries, so every site in the
curated table is regression-tested here regardless of which PR lands
first.

An integer literal in request/report JSON with more than 308 digits
passes ``is_number`` and then overflows ``float()`` -- an uncaught
``OverflowError`` traceback instead of the module's ``GenComposeError``
envelope (raising sites) or the documented "nothing usable" advisory
behaviour. Every site now guards with ``_jsonnum.is_finite_number``,
which also refuses NaN/±infinity floats that previously slipped through
the number check.
"""

from __future__ import annotations

import pytest

from klayout_tools import gen_compose
from klayout_tools.gen_compose import GenComposeError

HUGE = 10**400

NON_FINITE = [float("nan"), float("inf"), float("-inf")]


# ---------------------------------------------------------------------------
# cells[].ports[].x_um / .y_um  (_parse_cell_ports, raising)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
def test_cell_port_huge_position_is_a_contract_error(axis):
    """A >308-digit int in a hand-declared cell port position raises the
    existing ``GenComposeError`` wording, never an uncaught OverflowError."""
    port = {"name": "p", axis: HUGE}
    port["y_um" if axis == "x_um" else "x_um"] = 1.0
    with pytest.raises(GenComposeError) as excinfo:
        gen_compose._parse_cell_ports([port], "request.cells[0]")
    assert str(excinfo.value) == (
        "request.cells[0].ports[0] (name 'p') must declare both x_um and "
        "y_um as numbers, or neither"
    )


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
def test_cell_port_bool_position_is_a_contract_error(axis):
    """``bool`` stays refused (unchanged: it never passed ``is_number``)."""
    port = {"name": "p", axis: True}
    port["y_um" if axis == "x_um" else "x_um"] = 1.0
    with pytest.raises(GenComposeError):
        gen_compose._parse_cell_ports([port], "c")


def test_cell_port_finite_positions_still_convert():
    """Ordinary ints/floats still parse and convert exactly as before."""
    parsed = gen_compose._parse_cell_ports([{"name": "p", "x_um": 1, "y_um": 2.5}], "c")
    assert parsed[0]["x_um"] == 1.0
    assert parsed[0]["y_um"] == pytest.approx(2.5)


@pytest.mark.parametrize("value", NON_FINITE)
def test_cell_port_non_finite_position_is_a_contract_error(value):
    """NaN/±inf now take the not-a-number path (behaviour change, PR note)."""
    with pytest.raises(GenComposeError):
        gen_compose._parse_cell_ports([{"name": "p", "x_um": value, "y_um": 1.0}], "c")


# ---------------------------------------------------------------------------
# placement.row_pitch_um / .col_pitch_um  (_parse_array_placement, raising)
# ---------------------------------------------------------------------------


def _array_placement(field: str, value: object) -> dict:
    placement: dict = {
        "strategy": "array",
        "rows": 2,
        "cols": 2,
        "col_pitch_um": 1.0,
        "row_pitch_um": 1.0,
    }
    placement[field] = value
    return placement


@pytest.mark.parametrize("field", ["row_pitch_um", "col_pitch_um"])
def test_array_pitch_huge_int_is_a_contract_error(field):
    with pytest.raises(GenComposeError) as excinfo:
        gen_compose._parse_array_placement(_array_placement(field, HUGE))
    assert str(excinfo.value) == (
        f"request.placement.{field} must be a positive number when strategy is 'array'"
    )


@pytest.mark.parametrize("field", ["row_pitch_um", "col_pitch_um"])
@pytest.mark.parametrize("value", NON_FINITE + [True, -HUGE])
def test_array_pitch_non_finite_bool_or_negative_is_a_contract_error(field, value):
    """NaN/±inf are newly refused; ``True``/negative stayed refused."""
    with pytest.raises(GenComposeError):
        gen_compose._parse_array_placement(_array_placement(field, value))


def test_array_pitch_finite_values_still_convert():
    params = gen_compose._parse_array_placement(_array_placement("row_pitch_um", 10))
    assert params["row_pitch_um"] == 10.0
    assert params["col_pitch_um"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# placement.spacing_um  (_parse_placement, raising)
# ---------------------------------------------------------------------------


def test_spacing_um_huge_int_is_a_contract_error():
    with pytest.raises(GenComposeError) as excinfo:
        gen_compose._parse_placement(
            {"strategy": "row", "order": ["b"], "spacing_um": HUGE}, {"b"}
        )
    assert str(excinfo.value) == "request.placement.spacing_um must be a number"


@pytest.mark.parametrize("value", NON_FINITE + [True])
def test_spacing_um_non_finite_or_bool_is_a_contract_error(value):
    """NaN/±inf are newly refused (a NaN previously passed validation and
    propagated into placement math); ``True`` stayed refused."""
    with pytest.raises(GenComposeError) as excinfo:
        gen_compose._parse_placement(
            {"strategy": "row", "order": ["b"], "spacing_um": value}, {"b"}
        )
    assert str(excinfo.value) == "request.placement.spacing_um must be a number"


def test_spacing_um_ordinary_values_unchanged():
    _, _, spacing, *_ = gen_compose._parse_placement(
        {"strategy": "row", "order": ["b"], "spacing_um": 1.5}, {"b"}
    )
    assert spacing == pytest.approx(1.5)
    _, _, spacing, *_ = gen_compose._parse_placement(
        {"strategy": "row", "order": ["b"]}, {"b"}
    )
    assert spacing == 0.0
    with pytest.raises(GenComposeError) as excinfo:
        gen_compose._parse_placement(
            {"strategy": "row", "order": ["b"], "spacing_um": -3}, {"b"}
        )
    assert str(excinfo.value) == "request.placement.spacing_um must be >= 0"


# ---------------------------------------------------------------------------
# connectivity[].waypoints_um  (parse_waypoints_um, raising)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("axis", [0, 1])
def test_waypoint_huge_int_is_a_contract_error(axis):
    """A huge coordinate anywhere in a pair raises the existing wording --
    validation and the float() conversion live on separate lines here."""
    waypoints = [[0.0, 0.0], [HUGE, 1.0] if axis == 0 else [1.0, HUGE]]
    with pytest.raises(GenComposeError) as excinfo:
        gen_compose.parse_waypoints_um(waypoints, net="n", index=0)
    assert str(excinfo.value) == (
        "request.connectivity[0] (net 'n').waypoints_um[1] must be a "
        '[x_um, y_um] pair of numbers (or [x_um, y_um, "layer_role"] to '
        "change routing plane there)"
    )


def test_waypoint_triple_huge_int_is_a_contract_error():
    """The plane-change triple validates coordinates before the role check."""
    with pytest.raises(GenComposeError):
        gen_compose.parse_waypoints_um(
            [[0.0, 0.0], [HUGE, 1.0, "metal3"]],
            net="n",
            index=0,
            roles_out=[],
        )


@pytest.mark.parametrize("value", NON_FINITE + [True])
def test_waypoint_non_finite_or_bool_is_a_contract_error(value):
    with pytest.raises(GenComposeError):
        gen_compose.parse_waypoints_um([[0.0, value]], net="n", index=0)


def test_waypoints_finite_values_still_convert():
    parsed = gen_compose.parse_waypoints_um([[0, -3], [1.5, 2]], net="n", index=0)
    assert parsed == [(0.0, -3.0), (1.5, 2.0)]


# ---------------------------------------------------------------------------
# generator_report.drc_hints  (_parse_drc_hints, advisory)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["min_spacing_um", "foreign_clearance_um"])
@pytest.mark.parametrize("value", [HUGE, -HUGE] + NON_FINITE)
def test_drc_hints_unusable_number_is_nothing_claimed(field, value):
    """``drc_hints`` numbers are advisory (#434/#2638): a huge int or
    non-finite float is "nothing claimed", so the defaults survive."""
    matched, min_spacing, foreign = gen_compose._parse_drc_hints(
        {"matched_group_id": "g", field: value}
    )
    assert matched == "g"
    if field == "min_spacing_um":
        assert min_spacing == 0.0
        assert foreign is None
    else:
        assert min_spacing == 0.0
        assert foreign is None


def test_drc_hints_finite_values_still_read():
    matched, min_spacing, foreign = gen_compose._parse_drc_hints(
        {"matched_group_id": "g", "min_spacing_um": 2, "foreign_clearance_um": 0.5}
    )
    assert matched == "g"
    assert min_spacing == pytest.approx(2.0)
    assert foreign == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# generator_report port width_um  (_snap_port_to_grid, advisory)
# ---------------------------------------------------------------------------


def _snap_port(width: object) -> tuple[dict, list[str]]:
    port = {
        "name": "p",
        "x_um": 1.003,
        "y_um": 2.0,
        "width_um": width,
        "direction_deg": 0,
        "layer": {"layer": 68, "datatype": 20},
    }
    notes: list[str] = []
    snapped = gen_compose._snap_port_to_grid(
        port, {"block": "b", "port": "p"}, {"x": 0.0, "y": 0.0}, 0.005, notes
    )
    return snapped, notes


@pytest.mark.parametrize("width", [HUGE, -HUGE] + NON_FINITE)
def test_snap_port_unusable_width_is_unconstrained(width):
    """A generator-report port's ``width_um`` is trusted as-is (only
    ``name`` is checked by ``_parse_blocks``), so it can be a huge int or a
    non-finite float. The snap then treats the pad extent as unconstrained
    -- a finite snap target, never an uncaught OverflowError."""
    snapped, notes = _snap_port(width)
    assert snapped["x_um"] == pytest.approx(1.005)
    assert notes and "manufacturing grid" in notes[0]


def test_snap_port_finite_width_still_constrains():
    """A finite width still constrains the tangential snap: with a pad too
    narrow to hold an on-grid point, the attach point is left in place and
    reported as drawn off-grid, never moved outside the pad."""
    port = {
        "name": "p",
        "x_um": 1.0,
        "y_um": 2.001,
        "width_um": 0.001,
        "direction_deg": 0,
        "layer": {"layer": 68, "datatype": 20},
    }
    notes: list[str] = []
    snapped = gen_compose._snap_port_to_grid(
        port, {"block": "b", "port": "p"}, {"x": 0.0, "y": 0.0}, 0.005, notes
    )
    assert snapped["y_um"] == pytest.approx(2.001)
    assert notes and "holds no on-grid point" in notes[0]


# ---------------------------------------------------------------------------
# landing-connectivity probe position  (_port_probe_position_um, advisory)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("axis", ["x_um", "y_um"])
@pytest.mark.parametrize("value", [HUGE, -HUGE, True] + NON_FINITE)
def test_probe_position_unusable_value_is_none(axis, value):
    """The probe position is advisory: a huge int, a bool, or a non-finite
    float means "no usable numeric position" (``None``), never a raise."""
    port = {"x_um": 1.0, "y_um": 2.0}
    port[axis] = value
    assert gen_compose._port_probe_position_um(port) is None


def test_probe_position_finite_pair_survives():
    assert gen_compose._port_probe_position_um({"x_um": 1, "y_um": -2.5}) == (
        1.0,
        -2.5,
    )


def test_probe_position_missing_axes_are_none():
    assert gen_compose._port_probe_position_um({"x_um": 1.0}) is None
    assert gen_compose._port_probe_position_um({}) is None
