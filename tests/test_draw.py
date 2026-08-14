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


def test_draw_output_is_byte_reproducible(tmp_path):
    """Two `draw()` runs with identical params/inputs must produce
    byte-identical GDS streams (#320), matching `klt gen`'s reproducibility
    guarantee -- see `test_gen.test_generate_output_is_byte_reproducible`."""
    import time

    params = {
        "shapes": [
            {"layer": [66, 20], "name": "poly.drawing", "rect_um": [0, 0, 0.1, 2.0]}
        ]
    }

    _, output_a = _draw(tmp_path, params, name="a.gds")
    time.sleep(1.1)
    _, output_b = _draw(tmp_path, params, name="b.gds")

    from pathlib import Path

    assert Path(output_a).read_bytes() == Path(output_b).read_bytes()


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
    assert report["layers"] == [{"layer": 34, "datatype": 0, "name": None, "shapes": 1}]

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


# --------------------------------------------------------------------------- #
# `array`: repetition primitive on a shape entry (#553)
# --------------------------------------------------------------------------- #


def test_array_expands_rect_on_pitch(tmp_path):
    report, output = _draw(
        tmp_path,
        {
            "shapes": [
                {
                    "layer": [41, 0],
                    "rect_um": [0.0, 0.0, 0.2, 0.2],
                    "array": {"pitch_um": [1.0, 1.0], "count": [2, 3]},
                }
            ]
        },
    )

    # 2 x 3 = 6 instances -- both the top-level and per-layer counts must
    # reflect the expanded total, not the single JSON shape entry.
    assert report["shape_count"] == 6
    assert report["layers"] == [{"layer": 41, "datatype": 0, "name": None, "shapes": 6}]

    layout = kdb.Layout()
    layout.read(output)
    idx = layout.layer(41, 0)
    boxes = {s.box for s in layout.top_cell().shapes(idx).each() if s.is_box()}
    assert len(boxes) == 6
    expected = {
        kdb.Box(xi * 1000, yi * 1000, xi * 1000 + 200, yi * 1000 + 200)
        for xi in range(2)
        for yi in range(3)
    }
    assert boxes == expected


def test_array_expands_polygon_on_pitch(tmp_path):
    report, output = _draw(
        tmp_path,
        {
            "shapes": [
                {
                    "layer": [34, 0],
                    "polygon_um": [[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]],
                    "array": {"pitch_um": [0.86, 0.86], "count": [3, 2]},
                }
            ]
        },
    )
    assert report["shape_count"] == 6

    layout = kdb.Layout()
    layout.read(output)
    idx = layout.layer(34, 0)
    bboxes = {p.polygon.bbox() for p in layout.top_cell().shapes(idx).each()}
    assert len(bboxes) == 6
    pitch_dbu = 860  # round(0.86 / 0.001)
    expected = {
        kdb.Box(
            xi * pitch_dbu, yi * pitch_dbu, xi * pitch_dbu + 500, yi * pitch_dbu + 500
        )
        for xi in range(3)
        for yi in range(2)
    }
    assert bboxes == expected


def test_array_count_one_is_equivalent_to_no_array(tmp_path):
    """A `count: [1, 1]` array must produce byte-identical output to omitting
    `array` entirely -- the additive field must not change existing
    behaviour (#553 acceptance criteria)."""
    base_shape = {"layer": [1, 0], "rect_um": [0, 0, 1, 1]}

    _, output_no_array = _draw(
        tmp_path, {"shapes": [dict(base_shape)]}, name="no_array.gds"
    )
    _, output_with_array = _draw(
        tmp_path,
        {
            "shapes": [
                {**base_shape, "array": {"pitch_um": [5.0, 5.0], "count": [1, 1]}}
            ]
        },
        name="with_array.gds",
    )

    from pathlib import Path

    assert Path(output_no_array).read_bytes() == Path(output_with_array).read_bytes()


def test_array_omitted_shape_count_matches_pre_553_behaviour(tmp_path):
    report, _ = _draw(
        tmp_path,
        {
            "shapes": [
                {"layer": [1, 0], "rect_um": [0, 0, 1, 1]},
                {"layer": [2, 0], "rect_um": [0, 0, 1, 1]},
            ]
        },
    )
    assert report["shape_count"] == 2


def test_array_lands_exactly_on_pitch_regardless_of_float_accumulation(tmp_path):
    """Stepping is `origin + i * pitch` computed in integer database units
    after the unit shape is snapped -- not accumulated float `origin_um +
    i*pitch_um` before conversion -- so a pitch that is not exactly
    representable in binary float still lands exactly on `round(pitch_um /
    dbu_um)` dbu per step for every instance (#553)."""
    report, output = _draw(
        tmp_path,
        {
            "shapes": [
                {
                    "layer": [41, 0],
                    "rect_um": [0.0, 0.0, 0.26, 0.26],
                    "array": {"pitch_um": [0.86, 0.86], "count": [10, 1]},
                }
            ]
        },
    )
    assert report["shape_count"] == 10

    layout = kdb.Layout()
    layout.read(output)
    idx = layout.layer(41, 0)
    xs = sorted(s.box.left for s in layout.top_cell().shapes(idx).each() if s.is_box())
    pitch_dbu = 860  # round(0.86 / 0.001)
    assert xs == [i * pitch_dbu for i in range(10)]


def test_array_not_object_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"shape\[0\]\.array must be a JSON object"):
        _draw(
            tmp_path,
            {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1], "array": [1, 2]}]},
        )


def test_array_missing_pitch_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"array\.pitch_um must be"):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "array": {"count": [2, 2]},
                    }
                ]
            },
        )


def test_array_bad_pitch_length_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"array\.pitch_um must be"):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "array": {"pitch_um": [1.0], "count": [2, 2]},
                    }
                ]
            },
        )


def test_array_missing_count_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"array\.count must be"):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "array": {"pitch_um": [1.0, 1.0]},
                    }
                ]
            },
        )


def test_array_non_positive_count_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"array\.count must be"):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "array": {"pitch_um": [1.0, 1.0], "count": [0, 2]},
                    }
                ]
            },
        )


def test_array_non_integer_count_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"array\.count must be"):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "array": {"pitch_um": [1.0, 1.0], "count": [2.5, 2]},
                    }
                ]
            },
        )


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
# Unknown-key policy (#950): reject, with a reserved `_` annotation prefix
# --------------------------------------------------------------------------- #


def test_unknown_top_level_request_key_is_an_error(tmp_path):
    request = {
        "schema": REQUEST_SCHEMA,
        "params": {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
        "options": {"output": str(tmp_path / "out.gds")},
        "purpose": "poly.width.1 negative fixture",
    }
    with pytest.raises(DrawError, match=r"request has unknown key\(s\): purpose"):
        draw(request)


def test_unknown_params_key_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"params has unknown key\(s\): dbu_nm"):
        _draw(
            tmp_path,
            {"dbu_nm": 1, "shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
        )


def test_unknown_options_key_is_an_error(tmp_path):
    request = {
        "schema": REQUEST_SCHEMA,
        "params": {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}]},
        "options": {"output": str(tmp_path / "out.gds"), "cellname": "TOP"},
    }
    with pytest.raises(DrawError, match=r"options has unknown key\(s\): cellname"):
        draw(request)


def test_unknown_shape_key_is_an_error(tmp_path):
    """A typo in a real shape key is caught by name, not silently dropped."""
    with pytest.raises(DrawError, match=r"shape\[0\] has unknown key\(s\): rect_nm"):
        _draw(
            tmp_path,
            {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1], "rect_nm": []}]},
        )


def test_unknown_label_key_is_an_error(tmp_path):
    with pytest.raises(DrawError, match=r"label\[0\] has unknown key\(s\): at_nm"):
        _draw(
            tmp_path,
            {
                "shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1]}],
                "labels": [
                    {"layer": [1, 0], "text": "IN", "at_um": [0, 0], "at_nm": [0, 0]}
                ],
            },
        )


def test_unknown_array_key_is_an_error(tmp_path):
    with pytest.raises(
        DrawError, match=r"shape\[0\]\.array has unknown key\(s\): counts"
    ):
        _draw(
            tmp_path,
            {
                "shapes": [
                    {
                        "layer": [1, 0],
                        "rect_um": [0, 0, 1, 1],
                        "array": {"pitch_um": [2, 2], "counts": [2, 2]},
                    }
                ]
            },
        )


def test_unknown_key_error_names_the_allowed_keys_and_escape_hatch(tmp_path):
    """The error is actionable: it lists what *is* allowed and points at the
    documented `_`-prefixed annotation escape hatch (docs/cli/draw.md)."""
    with pytest.raises(DrawError) as excinfo:
        _draw(
            tmp_path,
            {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1], "rule": 1}]},
        )
    message = str(excinfo.value)
    assert "allowed: array, layer, name, polygon_um, rect_um" in message
    assert "keys beginning with '_' are reserved" in message


def test_annotation_keys_are_accepted_everywhere(tmp_path):
    """The motivating case (issue #950): a self-documenting known-bad DRC
    fixture carries its rationale inline via `_`-prefixed keys, at every
    nesting level, and still draws."""
    output = str(tmp_path / "annotated.gds")
    request = {
        "schema": REQUEST_SCHEMA,
        "_purpose": "negative control for sky130 poly.width.1",
        "_expected_rules": ["poly.width.1"],
        "params": {
            "_note": "dimensions are deliberately illegal -- do not 'fix' them",
            "shapes": [
                {
                    "layer": [66, 20],
                    "rect_um": [0, 0, 0.1, 2.0],
                    "_rule": "poly.width.1 (0.1 um < 0.15 um minimum)",
                },
                {
                    "layer": [41, 0],
                    "rect_um": [0, 0, 0.26, 0.26],
                    "array": {
                        "pitch_um": [0.86, 0.86],
                        "count": [2, 2],
                        "_rule": "via farm, on-pitch",
                    },
                },
            ],
            "labels": [
                {"layer": [66, 20], "text": "IN", "at_um": [0, 0], "_rule": "port"}
            ],
        },
        "options": {"output": output, "_rule": "written by the fixture generator"},
    }
    report = draw(request)
    assert report["shape_count"] == 5  # 1 rect + a 2x2 array
    assert report["label_count"] == 1


def test_annotation_keys_do_not_change_the_written_stream(tmp_path):
    """Annotations are inert: stripping them produces the same geometry."""
    shapes = [{"layer": [66, 20], "rect_um": [0, 0, 0.1, 2.0]}]
    plain, plain_path = _draw(tmp_path, {"shapes": shapes}, name="plain.gds")
    annotated, annotated_path = _draw(
        tmp_path,
        {
            "_purpose": "why this fixture exists",
            "shapes": [dict(shapes[0], _rule="poly.width.1")],
        },
        name="annotated.gds",
    )

    assert annotated["shape_count"] == plain["shape_count"]
    assert annotated["layers"] == plain["layers"]
    assert annotated["bbox_um"] == plain["bbox_um"]

    layout_plain = kdb.Layout()
    layout_plain.read(plain_path)
    layout_annotated = kdb.Layout()
    layout_annotated.read(annotated_path)
    assert (
        layout_annotated.top_cell().bbox().to_s()
        == layout_plain.top_cell().bbox().to_s()
    )


def test_unknown_key_is_rejected_before_anything_is_written(tmp_path):
    output = tmp_path / "out.gds"
    with pytest.raises(DrawError, match="unknown key"):
        _draw(
            tmp_path,
            {"shapes": [{"layer": [1, 0], "rect_um": [0, 0, 1, 1], "oops": True}]},
            name="out.gds",
        )
    assert not output.exists()


def test_cli_rejects_unknown_key_with_error_envelope(tmp_path, capsys):
    code = main(
        [
            "draw",
            "--params",
            '{"shapes": [{"layer": [66, 20], "rect_nm": [0, 0, 100, 2000]}]}',
            "-o",
            str(tmp_path / "out.gds"),
            "--format",
            "json",
        ]
    )
    assert code == 1
    error = json.loads(capsys.readouterr().err)
    assert "shape[0] has unknown key(s): rect_nm" in error["error"]["message"]


def test_cli_accepts_annotation_keys(tmp_path, capsys):
    output = str(tmp_path / "out.gds")
    code = main(
        [
            "draw",
            "--params",
            json.dumps(
                {
                    "_purpose": "poly.width.1 negative control",
                    "shapes": [
                        {
                            "layer": [66, 20],
                            "rect_um": [0, 0, 0.1, 2.0],
                            "_rule": "poly.width.1",
                        }
                    ],
                }
            ),
            "-o",
            output,
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shape_count"] == 1


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
