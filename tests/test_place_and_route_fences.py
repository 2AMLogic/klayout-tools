"""Opt-in placement fences must resolve every exact name and have final DEF evidence."""

import re
import shutil
import subprocess

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


# --- Live OpenROAD coverage of the fence Tcl (``_INSTALL_FENCE``) -----------
# The tests above only inspect the emitted text. These run the production
# ``placement_fence_lines()`` through ``openroad`` so a break in the OpenDB
# region/group construction, the escaped-bus-bracket lookup or the diagnostics
# fails here. They skip where the binary is absent (CI has no OpenROAD job).

needs_openroad = pytest.mark.skipif(
    shutil.which("openroad") is None, reason="OpenROAD is unavailable"
)

_FENCE_LEF = """VERSION 5.8 ;
UNITS DATABASE MICRONS 1000 ; END UNITS
SITE cell_site CLASS CORE ; SIZE 0.5 BY 2 ; END cell_site
MACRO BUF CLASS CORE ; ORIGIN 0 0 ; SIZE 1 BY 2 ; SYMMETRY X Y ;
    SITE cell_site ; END BUF
END LIBRARY
"""

# Both cells start in row1 far outside the fence, already legal, so the
# legalizer has no reason of its own to move them. ``dly[2]`` is a bracketed
# bus name (DEF spells it ``dly\[2\]``), the case ``klt_fence_instance`` indexes.
_FENCE_DEF = r"""VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 4000 ) ;
ROW row0 cell_site 0 0 N DO 20 BY 1 STEP 500 0 ;
ROW row1 cell_site 0 2000 FS DO 20 BY 1 STEP 500 0 ;
COMPONENTS 2 ;
- dly\[2\] BUF + PLACED ( 8000 2000 ) FS ;
- next BUF + PLACED ( 9000 2000 ) FS ;
END COMPONENTS
END DESIGN
"""

_FENCE_RECT_DBU = (1000, 0, 4000, 2000)  # region_um [1.0, 0.0, 4.0, 2.0] at 1000/um
_CELL_DBU = (1000, 2000)  # BUF is 1 x 2 um; no orientation here swaps width/height


def _run_fence_placement(tmp_path, region_um, *, fenced=True):
    """detailed_placement on the fixture, with or without the production fence."""
    (tmp_path / "cells.lef").write_text(_FENCE_LEF)
    (tmp_path / "design.def").write_text(_FENCE_DEF)
    groups = _validate_instance_groups(
        [
            {
                "name": "clock_side",
                "instances": ["dly[2]", "next"],
                "region_um": region_um,
                "enforce_region": True,
            }
        ]
    )
    lines = [
        "read_lef cells.lef",
        "read_def design.def",
        *(placement_fence_lines(groups) if fenced else []),
        "detailed_placement",
        "write_def final.def",
    ]
    (tmp_path / "place.tcl").write_text("\n".join(lines) + "\n")
    return subprocess.run(
        ["openroad", "-no_init", "-exit", "place.tcl"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )


def _placed_origins(def_text):
    """Instance name (as spelled in the DEF) -> placed origin in database units."""
    return {
        match["name"]: (int(match["x"]), int(match["y"]))
        for match in re.finditer(
            r"^\s*- (?P<name>\S+) BUF \+ PLACED \( (?P<x>\d+) (?P<y>\d+) \)",
            def_text,
            re.MULTILINE,
        )
    }


def _inside_fence(origin):
    x, y = origin
    x0, y0, x1, y1 = _FENCE_RECT_DBU
    width, height = _CELL_DBU
    return x0 <= x and y0 <= y and x + width <= x1 and y + height <= y1


@needs_openroad
def test_openroad_fence_moves_cells_into_region(tmp_path):
    run = _run_fence_placement(tmp_path, [1.0, 0.0, 4.0, 2.0])
    assert run.returncode == 0, run.stdout
    written = (tmp_path / "final.def").read_text()
    origins = _placed_origins(written)
    assert set(origins) == {r"dly\[2\]", "next"}, written
    assert all(_inside_fence(origin) for origin in origins.values()), origins
    # The fence itself must survive write_def, not merely the cell positions.
    assert "REGIONS 1 ;" in written
    assert "- klt_fence_0 ( 1000 0 ) ( 4000 2000 ) + TYPE FENCE ;" in written
    group = re.search(
        r"^\s*- klt_fence_0 (?P<members>.*?) \+ REGION klt_fence_0 ;",
        written,
        re.MULTILINE,
    )
    assert group is not None, written
    assert sorted(group["members"].split()) == [r"dly\[2\]", "next"]


@needs_openroad
def test_openroad_without_fence_leaves_cells_outside_region(tmp_path):
    # Control for the test above: the identical script minus the fence lines.
    # Without it the cells stay put, so their movement above came from the fence.
    run = _run_fence_placement(tmp_path, [1.0, 0.0, 4.0, 2.0], fenced=False)
    assert run.returncode == 0, run.stdout
    written = (tmp_path / "final.def").read_text()
    origins = _placed_origins(written)
    assert set(origins) == {r"dly\[2\]", "next"}, written
    assert not any(_inside_fence(origin) for origin in origins.values()), origins
    assert "REGIONS" not in written


@needs_openroad
def test_openroad_fence_too_small_for_cells_fails(tmp_path):
    # 0.5 um wide, but BUF is 1 um wide.
    run = _run_fence_placement(tmp_path, [1.0, 0.0, 1.5, 2.0])
    assert run.returncode != 0
    assert "placement fence 'clock_side' is too small for its requested cells" in (
        run.stdout
    )
    assert not (tmp_path / "final.def").exists()
