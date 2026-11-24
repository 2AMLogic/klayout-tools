"""Opt-in placement fences must resolve every exact name and have final DEF evidence."""

import pytest

from klayout_tools.place_and_route_fences import (
    checked_fence_reports,
    placement_fence_lines,
)
from klayout_tools.place_and_route_pins import read_def_instance_groups
from klayout_tools.place_and_route_request import (
    PlaceAndRouteError,
    _validate_instance_groups,
)
from test_place_and_route_pins import _COMPONENTS_DEF

GROUP = {"name": "pair", "instances": ["dly0", "dly[2]"], "region_um": [10, 20, 16, 22]}


@pytest.mark.parametrize(
    "update,diagnostic",
    [
        ({"enforce_region": "yes"}, "boolean"),
        ({"enforce_region": True, "region_um": None}, "requires region_um"),
        ({"enforce_region": True, "instances": ["dly*"]}, "exact names"),
    ],
)
def test_invalid_fence(update, diagnostic):
    with pytest.raises(PlaceAndRouteError, match=diagnostic):
        _validate_instance_groups([{**GROUP, **update}])


def test_only_enforced_groups_add_placement_commands():
    for entry in (GROUP, dict(GROUP, enforce_region=False)):
        assert placement_fence_lines(_validate_instance_groups([entry])) == []
    groups = _validate_instance_groups([dict(GROUP, enforce_region=True)])
    script = "\n".join(placement_fence_lines(groups))
    assert r"klt_install_fence pair [list dly0 dly\[2\]] 10 20 16 22" in script


@pytest.mark.parametrize("member", ["dly[2]", "far0", "ghost", "absent"])
def test_fence_requires_all_placed_origins_inside(tmp_path, member):
    path = tmp_path / "placed.def"
    path.write_text(_COMPONENTS_DEF)
    groups = _validate_instance_groups(
        [dict(GROUP, instances=["dly0", member], enforce_region=True)]
    )
    reports = read_def_instance_groups(str(path), groups)
    if member == "dly[2]":
        assert checked_fence_reports(reports, groups, PlaceAndRouteError) == reports
    else:
        with pytest.raises(PlaceAndRouteError, match="placement fence 'pair'"):
            checked_fence_reports(reports, groups, PlaceAndRouteError)


def test_missing_def_cannot_satisfy_fence():
    groups = _validate_instance_groups([dict(GROUP, enforce_region=True)])
    with pytest.raises(PlaceAndRouteError, match="placement fence 'pair'"):
        checked_fence_reports(None, groups, PlaceAndRouteError)
