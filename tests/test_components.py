"""Tests for `klt components` and the `run_components_report` library
function (issue #674).

Fixtures are generated programmatically with `klayout.db` inside the tests --
no dependency on an external corpus or PDK deck, mirroring `tests/test_ring_check.py`.
The core acceptance scenario (issue #674's "Measured, not assumed" table): two
crossing metals with no via stay two components; the same geometry with a via
joins them into exactly one; an unrelated, isolated metal island is still
reported even though it has no device or label.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.components import ComponentsError, run_components_report

_DBU = 0.005

_M1 = (68, 20)
_M2 = (69, 20)
_VIA = (70, 20)
_LABEL = (68, 5)

_CONDUCTORS = [
    {"name": "m1", "layer": [68, 20]},
    {"name": "m2", "layer": [69, 20]},
]
_VIAS = [{"name": "via1", "layer": [70, 20], "between": ["m1", "m2"]}]

# m1 is a horizontal strip, m2 a vertical strip -- they overlap in XY at
# (9000..11000, 4000..6000) but on different layers.
_M1_STRIP = kdb.Box(0, 4000, 20000, 6000)
_M2_STRIP = kdb.Box(9000, 0, 11000, 20000)
# A via exactly at the crossing -- landing on both m1 and m2.
_VIA_AT_CROSSING = kdb.Box(9000, 4000, 11000, 6000)
# An isolated island far away from the crossing, on m1 only.
_ISLAND = kdb.Box(30000, 30000, 32000, 32000)


def _write(path, layer_shapes: dict[tuple[int, int], kdb.Region | list]) -> None:
    """Write a single-top-cell layout with the given per-layer shapes.

    Each value is either a `kdb.Region` (its polygons are inserted) or a
    list of shapes (`kdb.Box`/`kdb.Text`/...) inserted as-is.
    """
    layout = kdb.Layout()
    layout.dbu = _DBU
    top = layout.create_cell("TOP")
    for (layer, datatype), shapes in layer_shapes.items():
        index = layout.layer(layer, datatype)
        layout.set_info(index, kdb.LayerInfo(layer, datatype, f"{layer}/{datatype}"))
        if isinstance(shapes, kdb.Region):
            for polygon in shapes.each():
                top.shapes(index).insert(polygon)
        else:
            for shape in shapes:
                top.shapes(index).insert(shape)
    layout.write(str(path))


def _write_crossing_with_island(path, *, with_via: bool) -> None:
    layers: dict[tuple[int, int], list] = {
        _M1: [_M1_STRIP, _ISLAND],
        _M2: [_M2_STRIP],
    }
    if with_via:
        layers[_VIA] = [_VIA_AT_CROSSING]
    _write(path, layers)


# --- Core acceptance scenario (issue #674) -----------------------------------


def test_no_via_crossing_metals_stay_two_components_plus_island(tmp_path):
    """TDD anchor: two crossing metals on different layers, no via -> the
    crossing stays two separate components (XY overlap alone never joins
    them), and the unrelated island is a third, independent component."""
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(str(path), _CONDUCTORS)

    assert report["schema_version"] == 1
    assert report["component_count"] == 3
    by_conductors = [
        tuple(sorted(c["name"] for c in comp["conductors"]))
        for comp in report["components"]
    ]
    assert sorted(by_conductors) == [("m1",), ("m1",), ("m2",)]


def test_via_at_crossing_joins_exactly_the_intended_two_components(tmp_path):
    """Adding a via at the crossing joins m1+m2 into one component; the
    unrelated island is untouched -- exactly two components remain."""
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)

    report = run_components_report(str(path), _CONDUCTORS, vias=_VIAS)

    assert report["component_count"] == 2
    joined = next(c for c in report["components"] if len(c["conductors"]) == 2)
    assert {c["name"] for c in joined["conductors"]} == {"m1", "m2"}
    assert [v["name"] for v in joined["vias"]] == ["via1"]
    island = next(c for c in report["components"] if len(c["conductors"]) == 1)
    assert island["conductors"][0]["name"] == "m1"
    assert island["vias"] == []


def test_isolated_island_reported_with_no_device_or_label(tmp_path):
    """The isolated metal island is reported as its own component even though
    it carries no via, no label, and no device (issue #674 acceptance)."""
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(str(path), _CONDUCTORS)

    island = next(
        c
        for c in report["components"]
        if c["bbox_um"]
        == {"left": 150.0, "bottom": 150.0, "right": 160.0, "top": 160.0}
    )
    assert [c["name"] for c in island["conductors"]] == ["m1"]
    assert island["labels"] == []
    assert island["vias"] == []


def test_via_shape_count_and_area_reported(tmp_path):
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)

    report = run_components_report(str(path), _CONDUCTORS, vias=_VIAS)

    joined = next(c for c in report["components"] if len(c["conductors"]) == 2)
    m1_entry = next(c for c in joined["conductors"] if c["name"] == "m1")
    # m1 strip (20000x2000 dbu) minus nothing -- one polygon.
    assert m1_entry["shape_count"] == 1
    assert m1_entry["area_um2"] == pytest.approx(100.0 * 10.0)
    via_entry = joined["vias"][0]
    assert via_entry["shape_count"] == 1
    assert via_entry["area_um2"] == pytest.approx(10.0 * 10.0)


# --- Names/labels/pins touched -----------------------------------------------


def test_label_text_touching_a_component_is_reported(tmp_path):
    path = tmp_path / "labelled.gds"
    _write(
        path,
        {
            _M1: [kdb.Box(0, 0, 10000, 10000)],
            _LABEL: [kdb.Text("VDD", kdb.Trans(1000, 1000))],
        },
    )
    conductors = [{"name": "m1", "layer": [68, 20]}]
    labels = [{"name": "m1_label", "layer": [68, 5]}]

    report = run_components_report(str(path), conductors, label_layers=labels)

    (component,) = report["components"]
    assert component["labels"] == ["VDD"]


def test_label_text_not_touching_any_component_is_absent(tmp_path):
    path = tmp_path / "labelled_far.gds"
    _write(
        path,
        {
            _M1: [kdb.Box(0, 0, 10000, 10000)],
            _LABEL: [kdb.Text("STRAY", kdb.Trans(50000, 50000))],
        },
    )
    conductors = [{"name": "m1", "layer": [68, 20]}]
    labels = [{"name": "m1_label", "layer": [68, 5]}]

    report = run_components_report(str(path), conductors, label_layers=labels)

    (component,) = report["components"]
    assert component["labels"] == []


# --- Crop boundary (--region) -------------------------------------------------


def test_component_touching_crop_boundary_is_flagged(tmp_path):
    path = tmp_path / "crop.gds"
    _write(path, {_M1: [kdb.Box(0, 0, 10000, 10000)]})
    conductors = [{"name": "m1", "layer": [68, 20]}]

    report = run_components_report(
        str(path), conductors, region_um=(0.0, 0.0, 5.0, 5.0)
    )

    (component,) = report["components"]
    assert component["bbox_um"] == {
        "left": 0.0,
        "bottom": 0.0,
        "right": 5.0,
        "top": 5.0,
    }
    assert component["touches_crop_boundary"] is True


def test_component_not_touching_crop_boundary_is_not_flagged(tmp_path):
    path = tmp_path / "no_crop_touch.gds"
    _write(path, {_M1: [kdb.Box(1000, 1000, 2000, 2000)]})
    conductors = [{"name": "m1", "layer": [68, 20]}]

    report = run_components_report(
        str(path), conductors, region_um=(0.0, 0.0, 50.0, 50.0)
    )

    (component,) = report["components"]
    assert component["touches_crop_boundary"] is False


def test_no_region_never_flags_crop_boundary(tmp_path):
    path = tmp_path / "no_crop.gds"
    _write(path, {_M1: [kdb.Box(0, 0, 10000, 10000)]})
    conductors = [{"name": "m1", "layer": [68, 20]}]

    report = run_components_report(str(path), conductors)

    assert report["region_um"] is None
    (component,) = report["components"]
    assert component["touches_crop_boundary"] is False


# --- Determinism ---------------------------------------------------------------


def test_output_is_deterministic_across_runs(tmp_path):
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)

    report_a = run_components_report(str(path), _CONDUCTORS, vias=_VIAS)
    report_b = run_components_report(str(path), _CONDUCTORS, vias=_VIAS)

    assert report_a == report_b
    ids = [c["id"] for c in report_a["components"]]
    assert ids == sorted(ids)


def test_json_output_is_deterministic_across_runs(tmp_path, capsys):
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)
    args = [
        "components",
        str(path),
        "--conductors",
        json.dumps(_CONDUCTORS),
        "--vias",
        json.dumps(_VIAS),
        "--format",
        "json",
    ]

    exit_code_a = main(args)
    out_a = capsys.readouterr().out
    exit_code_b = main(args)
    out_b = capsys.readouterr().out

    assert exit_code_a == 0
    assert exit_code_b == 0
    assert out_a == out_b


# --- Top cell / no PDK deck required ------------------------------------------


def test_no_pdk_deck_resolution_happens(tmp_path):
    """No `provenance`/`deck` field is ever emitted -- nothing is resolved
    when the caller supplies the stack explicitly (issue #674 acceptance)."""
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    report = run_components_report(str(path), _CONDUCTORS)

    assert "provenance" not in report
    assert "deck" not in report


def test_top_cell_selection(tmp_path):
    layout = kdb.Layout()
    layout.dbu = _DBU
    top_a = layout.create_cell("A")
    top_b = layout.create_cell("B")
    index = layout.layer(*_M1)
    layout.set_info(index, kdb.LayerInfo(*_M1))
    top_a.shapes(index).insert(kdb.Box(0, 0, 1000, 1000))
    top_b.shapes(index).insert(kdb.Box(0, 0, 2000, 2000))
    path = tmp_path / "multi_top.gds"
    layout.write(str(path))
    conductors = [{"name": "m1", "layer": [68, 20]}]

    report = run_components_report(str(path), conductors, top="A")

    assert report["component_count"] == 1
    assert report["components"][0]["cell"] == "A"


def test_unknown_top_cell_raises(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError):
        run_components_report(str(path), _CONDUCTORS, top="NOPE")


# --- Input validation ----------------------------------------------------------


def test_empty_conductors_raises(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError):
        run_components_report(str(path), [])


def test_duplicate_conductor_name_raises(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError):
        run_components_report(
            str(path),
            [
                {"name": "m1", "layer": [68, 20]},
                {"name": "m1", "layer": [69, 20]},
            ],
        )


def test_via_referencing_unknown_conductor_raises(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError):
        run_components_report(
            str(path),
            _CONDUCTORS,
            vias=[{"name": "v1", "layer": [70, 20], "between": ["m1", "nope"]}],
        )


def test_via_between_same_conductor_twice_raises(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    with pytest.raises(ComponentsError):
        run_components_report(
            str(path),
            _CONDUCTORS,
            vias=[{"name": "v1", "layer": [70, 20], "between": ["m1", "m1"]}],
        )


def test_missing_file_raises():
    with pytest.raises(ComponentsError):
        run_components_report("/no/such/file.gds", _CONDUCTORS)


# --- CLI -----------------------------------------------------------------------


def test_cli_text_output(tmp_path, capsys):
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)

    exit_code = main(
        [
            "components",
            str(path),
            "--conductors",
            json.dumps(_CONDUCTORS),
            "--vias",
            json.dumps(_VIAS),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "components: 2" in out


def test_cli_json_output(tmp_path, capsys):
    path = tmp_path / "with_via.gds"
    _write_crossing_with_island(path, with_via=True)

    exit_code = main(
        [
            "components",
            str(path),
            "--conductors",
            json.dumps(_CONDUCTORS),
            "--vias",
            json.dumps(_VIAS),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["component_count"] == 2


def test_cli_conductors_from_file(tmp_path, capsys):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)
    conductors_path = tmp_path / "conductors.json"
    conductors_path.write_text(json.dumps(_CONDUCTORS))

    exit_code = main(
        [
            "components",
            str(path),
            "--conductors",
            str(conductors_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["component_count"] == 3


def test_cli_bad_conductors_json_exit_one(tmp_path, capsys):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    exit_code = main(
        ["components", str(path), "--conductors", "not-json", "--format", "json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # JSON errors go to stderr, stdout stays empty
    error = json.loads(captured.err)
    assert error["error"]["command"] == "components"


def test_cli_missing_file_exit_one():
    exit_code = main(
        [
            "components",
            "/no/such/file.gds",
            "--conductors",
            json.dumps(_CONDUCTORS),
        ]
    )

    assert exit_code == 1


def test_cli_bad_region_exit_one(tmp_path):
    path = tmp_path / "no_via.gds"
    _write_crossing_with_island(path, with_via=False)

    exit_code = main(
        [
            "components",
            str(path),
            "--conductors",
            json.dumps(_CONDUCTORS),
            "--region",
            "[10, 0, 0, 10]",  # right <= left
        ]
    )

    assert exit_code == 1
