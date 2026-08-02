"""Tests for `klt draw` and the `draw` library function.

`klt draw` is the primitive, PDK-unaware write-side verb (issue #230): given a
JSON description of polygons/labels on explicit (layer, datatype) pairs, it
writes a stream verbatim, with no rule checking. Fixtures are read back with
`klayout.db` / `layers_report` to assert the geometry landed exactly where
requested; the headline scenario draws a rule-violating cell and asserts
`klt drc` flags it.
"""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.draw import (
    NOT_DESIGN_LEGAL_WARNING,
    REQUEST_SCHEMA,
    DrawError,
    draw,
    load_params_arg,
)


def _draw(tmp_path, params, cell_name=None, name="out.gds"):
    output = str(tmp_path / name)
    request = {
        "schema": REQUEST_SCHEMA,
        "params": params,
        "options": {"cell_name": cell_name, "output": output},
    }
    return draw(request), output


# --------------------------------------------------------------------------- #
# Round-trip: geometry lands exactly where requested
# --------------------------------------------------------------------------- #


def test_rect_round_trips_to_requested_layer_and_coords(tmp_path):
    report, output = _draw(
        tmp_path,
        {
            "shapes": [
                {"layer": [66, 20], "name": "poly.drawing", "rect_um": [0, 0, 0.1, 2.0]}
            ]
        },
    )

    assert report["schema_version"] == 1
    assert report["gds_path"] == output
    assert report["cell_name"] == "TOP"  # default
    assert report["shape_count"] == 1
    assert report["label_count"] == 0
    assert report["layers"] == [
        {"layer": 66, "datatype": 20, "name": "poly.drawing", "shapes": 1}
    ]
    assert report["bbox_um"] == {"x0": 0.0, "y0": 0.0, "x1": 0.1, "y1": 2.0}

    layout = kdb.Layout()
    layout.read(output)
    top = layout.top_cell()
    assert top.name == "TOP"
    idx = layout.layer(66, 20)
    boxes = [s.box for s in top.shapes(idx).each() if s.is_box()]
    assert len(boxes) == 1
    # dbu default 0.001 -> 0.1 um == 100 dbu, 2.0 um == 2000 dbu.
    assert boxes[0] == kdb.Box(0, 0, 100, 2000)


def test_polygon_round_trips(tmp_path):
    report, output = _draw(
        tmp_path,
        {
            "shapes": [
                {
                    "layer": [34, 0],
                    "polygon_um": [[0, 0], [1.0, 0], [1.0, 1.0], [0, 1.0]],
                }
            ]
        },
    )
    assert report["layers"] == [
        {"layer": 34, "datatype": 0, "name": None, "shapes": 1}
    ]

    layout = kdb.Layout()
    layout.read(output)
    idx = layout.layer(34, 0)
    polys = list(layout.top_cell().shapes(idx).each())
    assert len(polys) == 1
    assert polys[0].polygon.bbox() == kdb.Box(0, 0, 1000, 1000)


def test_label_written_at_requested_point(tmp_path):
    report, output = _draw(
        tmp_path,
        {
            "shapes": [{"layer": [66, 20], "rect_um": [0, 0, 1, 1]}],
            "labels": [{"layer": [66, 20], "text": "IN", "at_um": [0.5, 0.25]}],
        },
    )
    assert report["label_count"] == 1

    layout = kdb.Layout()
    layout.read(output)
    idx = layout.layer(66, 20)
    texts = [s.text for s in layout.top_cell().shapes(idx).each() if s.is_text()]
    assert len(texts) == 1
    assert texts[0].string == "IN"
    assert texts[0].position() == kdb.Point(500, 250)


def test_custom_cell_name_and_dbu(tmp_path):
    report, output = _draw(
        tmp_path,
        {"dbu_um": 0.005, "shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1.0, 1.0]}]},
        cell_name="MYCELL",
    )
    assert report["cell_name"] == "MYCELL"
    assert report["dbu_um"] == 0.005

    layout = kdb.Layout()
    layout.read(output)
    assert layout.top_cell().name == "MYCELL"
    assert layout.dbu == 0.005
    idx = layout.layer(1, 0)
    boxes = [s.box for s in layout.top_cell().shapes(idx).each() if s.is_box()]
    # 1.0 um / 0.005 um-per-dbu == 200 dbu.
    assert boxes[0] == kdb.Box(0, 0, 200, 200)


def test_reversed_rect_corners_are_normalised(tmp_path):
    report, output = _draw(
        tmp_path,
        {"shapes": [{"layer": [1, 0], "rect_um": [1.0, 1.0, 0.0, 0.0]}]},
    )
    assert report["bbox_um"] == {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}

    layout = kdb.Layout()
    layout.read(output)
    idx = layout.layer(1, 0)
    boxes = [s.box for s in layout.top_cell().shapes(idx).each() if s.is_box()]
    assert boxes[0] == kdb.Box(0, 0, 1000, 1000)


def test_multiple_shapes_same_layer_are_counted(tmp_path):
    report, _ = _draw(
        tmp_path,
        {
            "shapes": [
                {"layer": [1, 0], "rect_um": [0, 0, 1, 1]},
                {"layer": [1, 0], "rect_um": [2, 2, 3, 3]},
                {"layer": [2, 0], "rect_um": [0, 0, 1, 1]},
            ]
        },
    )
    counts = {(x["layer"], x["datatype"]): x["shapes"] for x in report["layers"]}
    assert counts == {(1, 0): 2, (2, 0): 1}


def test_layer_not_in_any_pdk_still_writes(tmp_path):
    """No PDK awareness is the point: an arbitrary (layer, datatype) succeeds."""
    report, output = _draw(
        tmp_path,
        {"shapes": [{"layer": [999, 7], "rect_um": [0, 0, 1, 1]}]},
    )
    assert report["layers"] == [
        {"layer": 999, "datatype": 7, "name": None, "shapes": 1}
    ]
    layout = kdb.Layout()
    layout.read(output)
    assert layout.layer(999, 7) is not None


def test_oasis_output_by_extension(tmp_path):
    _, output = _draw(
        tmp_path,
        {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
        name="out.oas",
    )
    layout = kdb.Layout()
    layout.read(output)  # opens without error -> OASIS was written
    idx = layout.layer(1, 0)
    assert layout.top_cell().shapes(idx).size() == 1


# --------------------------------------------------------------------------- #
# Self-labelling: every response carries the not-design-legal warning
# --------------------------------------------------------------------------- #


def test_response_stamped_not_design_legal(tmp_path):
    report, _ = _draw(
        tmp_path,
        {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
    )
    assert report["warnings"] == [NOT_DESIGN_LEGAL_WARNING]
    assert "not guaranteed to be design-legal" in NOT_DESIGN_LEGAL_WARNING


# --------------------------------------------------------------------------- #
# Validation / error paths
# --------------------------------------------------------------------------- #


def test_empty_shapes_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="must not be empty"):
        _draw(tmp_path, {"shapes": []})


def test_missing_shapes_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="params.shapes is required"):
        _draw(tmp_path, {})


def test_bad_layer_pair_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="layer must be a"):
        _draw(tmp_path, {"shapes": [{"layer": [1], "rect_um": [0, 0, 1, 1]}]})


def test_negative_layer_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="layer must be a"):
        _draw(tmp_path, {"shapes": [{"layer": [-1, 0], "rect_um": [0, 0, 1, 1]}]})


def test_two_geometry_keys_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="exactly one geometry key"):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "polygon_um": [[0, 0], [1, 0], [0, 1]],
                    }
                ]
            },
        )


def test_no_geometry_key_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="exactly one geometry key"):
        _draw(tmp_path, {"shapes": [{"layer": [1, 0]}]})


def test_bad_rect_length_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="rect_um must be"):
        _draw(tmp_path, {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1]}]})


def test_short_polygon_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="at least 3"):
        _draw(tmp_path, {"shapes": [{"layer": [1, 0], "polygon_um": [[0, 0], [1, 1]]}]})


def test_bad_dbu_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="dbu_um must be a positive number"):
        _draw(
            tmp_path,
            {"dbu_um": 0, "shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
        )


def test_label_missing_text_is_an_error(tmp_path):
    with pytest.raises(DrawError, match="label\\[0\\].text"):
        _draw(
            tmp_path,
            {
                "shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}],
                "labels": [{"layer": [1, 0], "at_um": [0, 0]}],
            },
        )


def test_output_dir_missing_is_an_error(tmp_path):
    request = {
        "schema": REQUEST_SCHEMA,
        "params": {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
        "options": {"output": str(tmp_path / "nope" / "out.gds")},
    }
    with pytest.raises(DrawError, match="output directory does not exist"):
        draw(request)


# --------------------------------------------------------------------------- #
# load_params_arg
# --------------------------------------------------------------------------- #


def test_load_params_inline():
    assert load_params_arg('{"shapes": []}') == {"shapes": []}


def test_load_params_from_file(tmp_path):
    p = tmp_path / "params.json"
    p.write_text('{"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]}')
    data = load_params_arg(str(p))
    assert data["shapes"][0]["layer"] == [1, 0]


def test_load_params_none_is_an_error():
    with pytest.raises(DrawError, match="--params is required"):
        load_params_arg(None)


def test_load_params_bad_json_is_an_error():
    with pytest.raises(DrawError, match="must be a path to a JSON file or an inline"):
        load_params_arg("{not json}")


def test_load_params_non_object_is_an_error():
    with pytest.raises(DrawError, match="must decode to a JSON object"):
        load_params_arg("[1, 2, 3]")


# --------------------------------------------------------------------------- #
# CLI wiring + JSON envelope
# --------------------------------------------------------------------------- #


def test_cli_json_envelope(tmp_path, capsys):
    output = str(tmp_path / "out.gds")
    code = main(
        [
            "draw",
            "--params",
            '{"shapes": [{"layer": [66, 20], "rect_um": [0, 0, 0.1, 2.0]}]}',
            "-o",
            output,
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["gds_path"] == output
    assert payload["shape_count"] == 1
    assert "warnings" in payload


def test_cli_text_output(tmp_path, capsys):
    output = str(tmp_path / "out.gds")
    code = main(
        [
            "draw",
            "--params",
            '{"shapes": [{"layer": [66, 20], "rect_um": [0, 0, 0.1, 2.0]}]}',
            "-o",
            output,
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert f"gds_path: {output}" in out
    assert "warnings:" in out


def test_cli_error_json_envelope(tmp_path, capsys):
    code = main(
        [
            "draw",
            "--params",
            '{"shapes": []}',
            "-o",
            str(tmp_path / "out.gds"),
            "--format",
            "json",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout empty on error under --format json
    error = json.loads(captured.err)
    assert error["error"]["command"] == "draw"
    assert "must not be empty" in error["error"]["message"]


def test_cli_missing_params_is_error(tmp_path, capsys):
    code = main(["draw", "-o", str(tmp_path / "out.gds"), "--format", "json"])
    assert code == 1
    error = json.loads(capsys.readouterr().err)
    assert "--params is required" in error["error"]["message"]


# --------------------------------------------------------------------------- #
# Headline scenario: a drawn violation is flagged by `klt drc`
# --------------------------------------------------------------------------- #


def test_drawn_violation_is_flagged_by_drc(tmp_path, capsys):
    """The scenario issue #230 exists to unblock: draw a too-narrow poly bar
    (0.1 um < sky130's 0.15 um poly.width.1 minimum), then run `klt drc` and
    assert the expected rule id fires with a non-clean exit code."""
    output = str(tmp_path / "bad.gds")
    draw_code = main(
        [
            "draw",
            "--params",
            json.dumps(
                {
                    "shapes": [
                        {
                            "layer": [66, 20],
                            "name": "poly.drawing",
                            "rect_um": [0, 0, 0.1, 2.0],
                        }
                    ]
                }
            ),
            "-o",
            output,
            "--format",
            "json",
        ]
    )
    assert draw_code == 0
    capsys.readouterr()  # drain

    drc_code = main(["drc", output, "--deck", "sky130", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert drc_code == 3  # deck ran and found violations (docs/cli/drc.md)
    assert payload["status"] == "violations"
    assert "poly.width.1" in payload["rule_counts"]
