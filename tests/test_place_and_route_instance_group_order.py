"""Tests for instance-group `region_um` / `order` (issue #4804)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route_groups import fixed_group_lines
from klayout_tools.place_and_route_pins import read_def_instance_groups
from klayout_tools.place_and_route_request import PlaceAndRouteError
from test_place_and_route import _setup_success_env, _stub_openroad_success
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


def test_fixed_group_allows_an_independent_measurement():
    order = {"axis": "y", "pitch_um": 2.72, "fixed_origin_um": [2.3, 5.44]}
    fixed, measured = _groups(
        {"name": "fixed", "instances": ["dly[2]", "dly[1]"], "order": order},
        {"name": "measure", "instances": ["dly[2]"], "order": {"axis": "x"}},
    )
    assert fixed["order"] == order
    assert measured["order"] == {"axis": "x"}


@pytest.mark.parametrize(
    "origin", [None, [], [0], [0, 0, 0], [False, 0], [0, float("inf")]]
)
def test_fixed_origin_requires_two_finite_coordinates(origin):
    with pytest.raises(PlaceAndRouteError, match="fixed_origin_um"):
        _groups(
            {
                "name": "row",
                "instances": ["a"],
                "order": {
                    "axis": "x",
                    "pitch_um": 2,
                    "fixed_origin_um": origin,
                },
            }
        )


def test_fixed_origin_requires_pitch():
    with pytest.raises(PlaceAndRouteError, match="pitch_um"):
        _groups(
            {
                "name": "row",
                "instances": ["a"],
                "order": {"axis": "x", "fixed_origin_um": [0, 0]},
            }
        )


@pytest.mark.parametrize("members", [[["a", "a"]], [["a"], ["a"]]])
def test_fixed_member_cannot_be_assigned_twice(members):
    order = {"axis": "x", "pitch_um": 1, "fixed_origin_um": [0, 0]}
    with pytest.raises(PlaceAndRouteError, match="group.*a.*already"):
        _groups(
            *[
                {"name": f"group{i}", "instances": v, "order": order}
                for i, v in enumerate(members)
            ]
        )


@pytest.mark.parametrize(
    ("axis", "last_coordinates"),
    [("x", "29 5 2 1"), ("y", "6 5 33 5")],
)
@pytest.mark.usefixtures("real_build_identity_git")
def test_fixed_members_render_before_global_placement(
    tmp_path, monkeypatch, axis, last_coordinates
):
    order = {"axis": axis, "pitch_um": 4.6, "fixed_origin_um": [1.2, 2]}
    path = _setup_success_env(
        tmp_path,
        monkeypatch,
        target_stage="place",
        instance_groups=[
            {"name": "chain", "instances": ["a", "dly[2]"], "order": order},
        ],
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))
    place_and_route.run_place_and_route(path)
    root = Path(path).parent / ".klt/place-and-route"
    assert "klt_fix_member" not in (root / "pnr_gcd_floorplan.tcl").read_text()
    script = (root / "pnr_gcd_place.tcl").read_text()
    assert script.index("klt_fixed_groups 0\n") < script.index(
        "global_placement -density"
    )
    assert script.index("detailed_placement\n") < script.index("klt_fixed_groups 1\n")
    assert (
        script.index("klt_fix_member $block $verify chain a 6 5 2 1")
        < script.index(
            rf"klt_fix_member $block $verify chain dly\[2\] {last_coordinates}"
        )
        < script.index("global_placement -density")
    )


@pytest.mark.usefixtures("real_build_identity_git")
def test_measurement_only_group_adds_no_placement_commands(tmp_path, monkeypatch):
    _stub_openroad_success(monkeypatch, stages=("floorplan", "place"))
    path = _setup_success_env(tmp_path, monkeypatch, target_stage="place")
    place_and_route.run_place_and_route(path)
    script = Path(path).parent / ".klt/place-and-route/pnr_gcd_place.tcl"
    baseline = script.read_bytes()
    request = json.loads(Path(path).read_text())
    request["instance_groups"] = [
        {"name": "chain", "instances": ["a"], "order": {"axis": "x", "pitch_um": 2}}
    ]
    Path(path).write_text(json.dumps(request))
    place_and_route.run_place_and_route(path)
    assert script.read_bytes() == baseline


@pytest.mark.skipif(shutil.which("openroad") is None, reason="OpenROAD is unavailable")
@pytest.mark.parametrize(
    ("names", "origin", "pitch", "diagnostic"),
    [
        (["dly[2]", "next"], [1, 0], 2, None),
        (["absent"], [1, 0], 2, "unknown linked instance"),
        (["dly[2]", "next"], [1, 0], 0.5, "overlaps fixed instance 'dly[2]'"),
        (["next"], [1.1, 0], 2, "no legal row/site"),
    ],
)
def test_openroad_fixed_sites(tmp_path, names, origin, pitch, diagnostic):
    (tmp_path / "cells.lef").write_text(
        """VERSION 5.8 ;
UNITS DATABASE MICRONS 1000 ; END UNITS
SITE cell_site CLASS CORE ; SIZE 0.5 BY 2 ; END cell_site
MACRO BUF CLASS CORE ; ORIGIN 0 0 ; SIZE 1 BY 2 ; SYMMETRY X Y ;
    SITE cell_site ; END BUF
END LIBRARY
"""
    )
    (tmp_path / "design.def").write_text(
        r"""VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 4000 ) ;
ROW row0 cell_site 0 0 N DO 20 BY 1 STEP 500 0 ;
ROW row1 cell_site 0 2000 FS DO 20 BY 1 STEP 500 0 ;
COMPONENTS 2 ;
- dly\[2\] BUF ;
- next BUF ;
END COMPONENTS
END DESIGN
"""
    )
    group = {
        "name": "chain",
        "instances": names,
        "order": {
            "axis": "x" if diagnostic else "y",
            "pitch_um": pitch,
            "fixed_origin_um": origin,
        },
    }
    lines = [
        "read_lef cells.lef",
        "read_def design.def",
        *fixed_group_lines(_groups(group)),
        "klt_fixed_groups 0",
        "klt_fixed_groups 1",
        "write_def final.def",
    ]
    (tmp_path / "place.tcl").write_text("\n".join(lines) + "\n")
    run = subprocess.run(
        ["openroad", "-no_init", "-exit", "place.tcl"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )
    if diagnostic:
        assert run.returncode != 0
        assert (
            f"instance group 'chain', member '{names[-1]}': {diagnostic}" in run.stdout
        )
    else:
        assert run.returncode == 0, run.stdout
        written = (tmp_path / "final.def").read_text()
        assert r"- dly\[2\] BUF + FIXED ( 1000 0 ) N" in written
        assert "- next BUF + FIXED ( 1000 2000 ) FS" in written
