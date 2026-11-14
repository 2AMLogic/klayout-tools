"""`klt components --probe`: point-to-component lookups (issue #5552).

Lives beside `test_components.py` rather than inside it: that module is over
the repository's per-file line limit and may not grow. Fixtures are reused
from it, the way `tests/test_stats_geometry_digest.py` reuses `test_stats`'.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.components import ComponentsError, run_components_report
from test_components import (
    _CONDUCTORS,
    _DBU,
    _M1,
    _VIAS,
    _write,
    _write_crossing_with_island,
)

# Two interleaved m1 rails: an "L" each, mirrored, so their bounding boxes
# overlap almost completely while the geometry never touches. `bbox_um` alone
# therefore cannot tell a caller which rail a point lands on -- the gap issue
# #5552 is about.
_RAIL_A = [kdb.Box(0, 0, 18000, 1000), kdb.Box(0, 0, 1000, 10000)]
_RAIL_B = [kdb.Box(2000, 9000, 20000, 10000), kdb.Box(19000, 0, 20000, 10000)]

_M1_ONLY = [{"name": "m1", "layer": list(_M1)}]


def _write_interleaved_rails(path) -> None:
    _write(path, {_M1: _RAIL_A + _RAIL_B})


def _probe(name: str, layer: str, x: float, y: float) -> dict:
    return {"name": name, "layer": layer, "x": x, "y": y}


def _probes_by_name(report: dict) -> dict:
    return {entry["name"]: entry for entry in report["probes"]}


def test_probes_absent_report_an_empty_list(tmp_path):
    """TDD anchor: `probes` is always present and additive -- a run with no
    `--probe` differs from today's output only by `"probes": []`, with
    `schema_version` still `1`."""
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(str(path), _CONDUCTORS)

    assert report["schema_version"] == 1
    assert report["probes"] == []


def test_probes_on_interleaved_rails_resolve_to_different_components(tmp_path):
    """TDD anchor for the core gap: two points whose *bounding boxes* both
    contain them land on different islands, and the probes say so."""
    path = tmp_path / "rails.gds"
    _write_interleaved_rails(path)

    report = run_components_report(
        str(path),
        _M1_ONLY,
        probes=[_probe("a", "m1", 45.0, 2.5), _probe("b", "m1", 45.0, 47.5)],
    )

    assert report["component_count"] == 2
    # Both points sit inside *both* components' bounding boxes -- the premise
    # of the issue -- so only a point lookup can separate them.
    for component in report["components"]:
        box = component["bbox_um"]
        for x, y in ((45.0, 2.5), (45.0, 47.5)):
            assert box["left"] <= x <= box["right"]
            assert box["bottom"] <= y <= box["top"]

    probes = _probes_by_name(report)
    assert probes["a"]["found"] is True
    assert probes["b"]["found"] is True
    assert probes["a"]["component_id"] != probes["b"]["component_id"]
    ids = {component["id"] for component in report["components"]}
    assert {probes["a"]["component_id"], probes["b"]["component_id"]} == ids


def test_probe_entry_shape_and_request_order(tmp_path):
    path = tmp_path / "rails.gds"
    _write_interleaved_rails(path)

    report = run_components_report(
        str(path),
        _M1_ONLY,
        probes=[_probe("second", "m1", 45.0, 47.5), _probe("first", "m1", 45.0, 2.5)],
    )

    assert [entry["name"] for entry in report["probes"]] == ["second", "first"]
    assert set(report["probes"][0]) == {
        "name",
        "layer",
        "x_um",
        "y_um",
        "found",
        "component_id",
    }
    assert report["probes"][0]["layer"] == "m1"
    assert report["probes"][0]["x_um"] == 45.0
    assert report["probes"][0]["y_um"] == 47.5


def test_probes_joined_through_a_via_share_one_component_id(tmp_path):
    """Two probes on different conductors joined by a via agree; the same two
    points without the via do not."""
    joined_path = tmp_path / "with_via.gds"
    _write_crossing_with_island(joined_path, with_via=True)
    probes = [_probe("lo", "m1", 10.0, 25.0), _probe("hi", "m2", 50.0, 80.0)]

    joined = _probes_by_name(
        run_components_report(str(joined_path), _CONDUCTORS, vias=_VIAS, probes=probes)
    )
    assert joined["lo"]["found"] is True
    assert joined["hi"]["found"] is True
    assert joined["lo"]["component_id"] == joined["hi"]["component_id"]

    split_path = tmp_path / "no_via.gds"
    _write_crossing_with_island(split_path, with_via=False)
    split = _probes_by_name(
        run_components_report(str(split_path), _CONDUCTORS, probes=probes)
    )
    assert split["lo"]["component_id"] != split["hi"]["component_id"]


def test_probe_component_id_is_a_reported_component(tmp_path):
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)

    report = run_components_report(
        str(path),
        _CONDUCTORS,
        vias=_VIAS,
        probes=[_probe("island", "m1", 155.0, 155.0)],
    )

    entry = report["probes"][0]
    owner = next(c for c in report["components"] if c["id"] == entry["component_id"])
    assert owner["bbox_um"] == {
        "left": 150.0,
        "bottom": 150.0,
        "right": 160.0,
        "top": 160.0,
    }


def test_probe_on_empty_space_is_not_found_not_an_error(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(
        str(path), _CONDUCTORS, probes=[_probe("void", "m1", 500.0, 500.0)]
    )

    assert report["probes"] == [
        {
            "name": "void",
            "layer": "m1",
            "x_um": 500.0,
            "y_um": 500.0,
            "found": False,
            "component_id": None,
        }
    ]


def test_probe_on_a_different_conductor_only_is_not_found(tmp_path):
    """The point sits on m1, but the probe names m2 -- an unrelated net
    crossing overhead on another layer must not match."""
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(
        str(path), _CONDUCTORS, probes=[_probe("wrong", "m2", 10.0, 25.0)]
    )

    assert report["probes"][0]["found"] is False
    assert report["probes"][0]["component_id"] is None


def test_probe_outside_the_region_crop_is_not_found(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(
        str(path),
        _CONDUCTORS,
        region_um=(0.0, 0.0, 20.0, 40.0),
        probes=[
            _probe("inside", "m1", 10.0, 25.0),
            _probe("outside", "m1", 90.0, 25.0),
        ],
    )

    probes = _probes_by_name(report)
    assert probes["inside"]["found"] is True
    assert probes["outside"]["found"] is False
    assert probes["outside"]["component_id"] is None


def _write_instanced_child(path) -> None:
    """A layout whose only m1 geometry lives in an instanced child cell,
    placed at a displacement in the top cell's frame."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    index = layout.layer(*_M1)
    layout.set_info(index, kdb.LayerInfo(*_M1))
    child.shapes(index).insert(kdb.Box(0, 0, 2000, 2000))
    top.insert(
        kdb.CellInstArray(child.cell_index(), kdb.Trans(kdb.Vector(30000, 30000)))
    )
    layout.write(str(path))


def test_probe_inside_an_instanced_child_cell_agrees_with_components(tmp_path):
    """A conductor shape drawn inside an instanced child cell is reported as
    part of a top-cell component (the engine flattens with
    `begin_shapes_rec`), and a probe at that point returns that component's
    id -- the hierarchy acceptance criterion of issue #5552."""
    path = tmp_path / "child.gds"
    _write_instanced_child(path)

    report = run_components_report(
        str(path),
        _M1_ONLY,
        probes=[_probe("pad", "m1", 155.0, 155.0), _probe("origin", "m1", 5.0, 5.0)],
    )

    assert report["component_count"] == 1
    component = report["components"][0]
    assert component["cell"] == "TOP"
    probes = _probes_by_name(report)
    assert probes["pad"]["found"] is True
    assert probes["pad"]["component_id"] == component["id"]
    # The child's own untranslated frame is empty in the top cell's frame.
    assert probes["origin"]["found"] is False


def test_probe_on_a_negative_coordinate(tmp_path):
    path = tmp_path / "negative.gds"
    _write(path, {_M1: [kdb.Box(-4000, -4000, -2000, -2000)]})

    report = run_components_report(
        str(path), _M1_ONLY, probes=[_probe("neg", "m1", -15.0, -15.0)]
    )

    assert report["probes"][0]["found"] is True
    assert report["probes"][0]["component_id"] == report["components"][0]["id"]


# --- Probe validation errors ------------------------------------------------


def test_probe_layer_must_name_a_declared_conductor(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError) as excinfo:
        run_components_report(
            str(path), _CONDUCTORS, probes=[_probe("a", "m9", 1.0, 1.0)]
        )

    message = str(excinfo.value)
    assert "m9" in message
    # The message lists the declared conductor names.
    assert "m1" in message and "m2" in message


def test_duplicate_probe_name_raises(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError, match="dup"):
        run_components_report(
            str(path),
            _CONDUCTORS,
            probes=[_probe("dup", "m1", 1.0, 1.0), _probe("dup", "m1", 2.0, 2.0)],
        )


@pytest.mark.parametrize("bad", ["x", None, True, [1.0]])
def test_non_numeric_probe_coordinate_raises(tmp_path, bad):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)
    probe = _probe("a", "m1", 1.0, 1.0)
    probe["x"] = bad

    with pytest.raises(ComponentsError, match="'a'"):
        run_components_report(str(path), _CONDUCTORS, probes=[probe])


@pytest.mark.parametrize(
    "probe",
    [{}, {"name": "a"}, "name=a", {"name": "", "layer": "m1", "x": 1.0, "y": 1.0}],
)
def test_malformed_probe_entry_raises(tmp_path, probe):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError):
        run_components_report(str(path), _CONDUCTORS, probes=[probe])


def _write_two_tops(path) -> None:
    """A two-top-cell stream, each top carrying one m1 square."""
    layout = kdb.Layout()
    layout.dbu = _DBU
    index = layout.layer(*_M1)
    layout.set_info(index, kdb.LayerInfo(*_M1))
    for name in ("A", "B"):
        layout.create_cell(name).shapes(index).insert(kdb.Box(0, 0, 1000, 1000))
    layout.write(str(path))


def test_probe_with_more_than_one_top_cell_in_scope_raises(tmp_path):
    path = tmp_path / "multi_top.gds"
    _write_two_tops(path)

    with pytest.raises(ComponentsError, match="--top"):
        run_components_report(str(path), _M1_ONLY, probes=[_probe("a", "m1", 1.0, 1.0)])

    # With --top naming one of them, the probe resolves normally.
    report = run_components_report(
        str(path), _M1_ONLY, top="A", probes=[_probe("a", "m1", 2.5, 2.5)]
    )
    assert report["probes"][0]["component_id"] == "A:0"


def test_probe_on_a_polygon_edge_resolves_to_that_component(tmp_path):
    """Documents KLayout's hit test at the boundary (the issue's edge case).

    `probe_net`'s test is *inclusive*: a point exactly on a shape's edge
    resolves to that shape's component, and so does a point one database unit
    outside it. Two database units out is `found: false`. A probe is
    therefore not a sub-DBU geometry assertion -- use `klt drc` for that.
    """
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)
    # The m1 strip's bottom edge is y = 4000 dbu = 20.0 um (`_DBU` = 0.005).
    on_edge, one_dbu_out, two_dbu_out = 20.0, 20.0 - _DBU, 20.0 - 2 * _DBU

    report = run_components_report(
        str(path),
        _CONDUCTORS,
        probes=[
            _probe("on_edge", "m1", 50.0, on_edge),
            _probe("one_out", "m1", 50.0, one_dbu_out),
            _probe("two_out", "m1", 50.0, two_dbu_out),
        ],
    )

    probes = _probes_by_name(report)
    strip = next(
        component
        for component in report["components"]
        if component["bbox_um"]["right"] == 100.0
    )
    assert probes["on_edge"]["component_id"] == strip["id"]
    assert probes["one_out"]["component_id"] == strip["id"]
    assert probes["two_out"]["found"] is False


# --- `--probe` through the CLI ----------------------------------------------


def _cli(path, *extra: str, conductors=_M1_ONLY) -> int:
    """`klt components <path> --conductors <conductors> <extra...>`."""
    argv = ["components", str(path), "--conductors", json.dumps(conductors)]
    return main([*argv, *extra])


def test_cli_probe_json_reports_component_ids(tmp_path, capsys):
    path = tmp_path / "rails.gds"
    _write_interleaved_rails(path)

    exit_code = _cli(
        path,
        "--probe",
        "name=a,layer=m1,x=45.0,y=2.5",
        "--probe",
        "name=b,layer=m1,x=45.0,y=47.5",
        "--format",
        "json",
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in report["probes"]] == ["a", "b"]
    ids = {entry["component_id"] for entry in report["probes"]}
    assert len(ids) == 2
    assert ids <= {component["id"] for component in report["components"]}


def test_cli_probe_text_prints_a_row_per_probe(tmp_path, capsys):
    path = tmp_path / "rails.gds"
    _write_interleaved_rails(path)

    exit_code = _cli(
        path,
        "--probe",
        "name=a,layer=m1,x=45.0,y=2.5",
        "--probe",
        "name=void,layer=m1,x=500.0,y=500.0",
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "probes: 2" in out
    assert "TOP:" in out
    # The not-found probe renders its component column as `-`.
    assert "void" in out


@pytest.mark.parametrize(
    "spec",
    [
        "name=a",  # missing keys
        "name=a,layer=m1,x=nope,y=1",  # non-numeric coordinate
        "name=a,layer=m9,x=1,y=1",  # `layer` is not a declared conductor
        "name=a,layer=m1,x=1,y=1,extra=2",  # unknown key
        "name=a,layer=m1,x=1,y=1,x=2",  # repeated key
        "name=a layer=m1 x=1 y=1",  # not key=value segments at all
    ],
)
def test_cli_malformed_or_unknown_probe_exits_one(tmp_path, spec):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    assert _cli(path, "--probe", spec, conductors=_CONDUCTORS) == 1


def test_cli_duplicate_probe_name_exits_one(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    assert (
        _cli(
            path,
            "--probe",
            "name=a,layer=m1,x=1,y=1",
            "--probe",
            "name=a,layer=m2,x=2,y=2",
            conductors=_CONDUCTORS,
        )
        == 1
    )


def test_cli_probe_error_uses_the_json_error_envelope(tmp_path, capsys):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    exit_code = _cli(
        path,
        "--probe",
        "name=a,layer=m9,x=1,y=1",
        "--format",
        "json",
        conductors=_CONDUCTORS,
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["schema_version"] == 1
    assert envelope["error"]["command"] == "components"
    assert "m9" in envelope["error"]["message"]


def test_cli_probe_with_more_than_one_top_cell_exits_one(tmp_path):
    path = tmp_path / "multi_top.gds"
    _write_two_tops(path)
    probe = ("--probe", "name=a,layer=m1,x=2.5,y=2.5")

    assert _cli(path, *probe) == 1
    assert _cli(path, *probe, "--top", "A") == 0
