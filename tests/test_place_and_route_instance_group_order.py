"""Tests for instance-group `region_um` / `order` (issue #4804)."""

from __future__ import annotations

import re

import pytest

from klayout_tools.place_and_route_pins import read_def_instance_groups
from klayout_tools.place_and_route_request import PlaceAndRouteError
from test_place_and_route_pins import _COMPONENTS_DEF, _groups


def test_region_and_order_measurement(tmp_path):
    path = tmp_path / "d.def"
    path.write_text(_COMPONENTS_DEF)
    names = ["dly0", "dly1", "dly[2]"]
    groups = _groups(
        {
            "name": "ok",
            "instances": names,
            "region_um": [10, 20, 16, 22],
            "order": {"axis": "x", "pitch_um": 3},
        },
        {
            "name": "bad",
            "instances": names,
            "region_um": [10, 20, 15.9, 22],
            "order": {"axis": "x", "pitch_um": 2},
        },
        {"name": "rev", "instances": names[::-1], "order": {"axis": "x"}},
        {"name": "yaxis", "instances": ["dly0", "far0"], "order": {"axis": "y"}},
        {
            "name": "unplaced",
            "instances": ["dly0", "ghost"],
            "region_um": [0, 0, 100, 100],
            "order": {"axis": "x"},
        },
        {"name": "plain", "instances": names},
    )
    res = {g["name"]: g for g in read_def_instance_groups(str(path), groups)}
    assert res["ok"]["within_region"] is True
    assert res["ok"]["order"] == {
        "axis": "x",
        "members": names,
        "ordered": True,
        "pitch_um_measured": [3.0, 3.0],
        "pitch_um": 3,
        "pitch_ok": True,
    }
    assert res["bad"]["within_region"] is False
    assert res["bad"]["order"]["pitch_ok"] is False
    assert res["rev"]["order"]["ordered"] is True  # descending is monotonic
    assert res["rev"]["order"]["pitch_ok"] is None
    assert res["yaxis"]["order"]["ordered"] is True
    assert res["unplaced"]["order"]["ordered"] is None
    assert res["unplaced"]["order"]["pitch_um_measured"] == []
    assert res["unplaced"]["within_region"] is None
    assert "within_region" not in res["plain"]
    assert "order" not in res["plain"]


def test_order_detects_misordering(tmp_path):
    path = tmp_path / "d.def"
    path.write_text(_COMPONENTS_DEF)
    (g,) = read_def_instance_groups(
        str(path),
        _groups(
            {
                "name": "g",
                "instances": ["dly0", "dly[2]", "dly1"],
                "order": {"axis": "x"},
            }
        ),
    )
    assert g["order"]["ordered"] is False


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ({"region_um": [0, 0, 1]}, "instance_groups[0].region_um"),
        ({"region_um": [1, 0, 1, 2]}, "instance_groups[0].region_um"),
        ({"region_um": [0, 0, True, 2]}, "instance_groups[0].region_um"),
        ({"region_um": [0, 0, float("inf"), 2]}, "instance_groups[0].region_um"),
        ({"order": "x"}, "instance_groups[0].order"),
        ({"order": {"axis": "z"}}, "instance_groups[0].order.axis"),
        ({"order": {"axis": "x", "pitch_um": 0}}, "order.pitch_um"),
        ({"order": {"axis": "x", "bogus": 1}}, "order has unknown key"),
        ({"order": {"axis": "x"}, "instances": ["dly*"]}, "no glob"),
    ],
)
def test_region_order_validation_rejects(entry, needle):
    base = {"name": "g", "instances": ["a", "b"]}
    with pytest.raises(PlaceAndRouteError, match=re.escape(needle)):
        _groups({**base, **entry})
