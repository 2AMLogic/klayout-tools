"""Placement checking through Compose and the public command line."""

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.gen_compose import GenComposeError, compose
from test_gen_compose import _isolate as _isolate
from test_gen_compose import _write_gds_with_labeled_pad
from test_gen_compose import pdk_root as pdk_root


def _gate_block(tmp_path, block_id, bbox_um, drawn_um):
    """A #6942 gate fixture: a fake ``generator_report`` block whose single
    drawn li1 pad sits at ``drawn_um`` ``(x0, y0, x1, y1)`` in the cell's own
    local frame, with a declared ``bbox_um`` the caller controls -- keep the
    pad inside the declaration for a growth-clean block (only the pairwise
    rule can fire), or let it stick out to grow the real footprint on
    purpose."""
    gds_path = _write_gds_with_labeled_pad(
        tmp_path / f"{block_id}.gds", f"{block_id}_top", drawn_um, block_id.upper()
    )
    return {
        "generator": f"fake_{block_id}",
        "cell_name": f"{block_id}_top",
        "gds_path": gds_path,
        "bbox_um": dict(bbox_um),
        "ports": [],
    }


def test_compose_check_placement_positive_area_overlap_fails(tmp_path, pdk_root):
    a = _gate_block(
        tmp_path,
        "a",
        {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        (1.0, 1.0, 9.0, 9.0),
    )
    b = _gate_block(
        tmp_path,
        "b",
        {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 3.0},
        (0.2, 0.2, 2.8, 2.8),
    )
    output = tmp_path / "gate_overlap.gds"
    with pytest.raises(GenComposeError) as excinfo:
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "a", "generator_report": a},
                    {"id": "b", "generator_report": b},
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["a", "b"],
                    "origins_um": {
                        "a": {"x": 0.0, "y": 0.0},
                        "b": {"x": 9.0, "y": 2.0},
                    },
                },
                "options": {
                    "cell_name": "gate_overlap",
                    "output": str(output),
                    "check_placement": True,
                },
            }
        )
    message = str(excinfo.value)
    assert "'a'" in message
    assert "'b'" in message
    assert "(9.000, 2.000)-(10.000, 5.000)" in message
    assert "grew" not in message  # both declarations match their geometry
    assert not output.exists()  # refuses before creating the output


def test_compose_check_placement_off_preserves_existing_behavior(tmp_path, pdk_root):
    # The same overlapping placement with the option absent and explicitly
    # false: compose still succeeds (a caller-declared overlap remains legal
    # input -- the gate is opt-in, the advisories unchanged).
    a = _gate_block(
        tmp_path,
        "a",
        {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        (1.0, 1.0, 9.0, 9.0),
    )
    b = _gate_block(
        tmp_path,
        "b",
        {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 3.0},
        (0.2, 0.2, 2.8, 2.8),
    )
    for check_value in (None, False):
        options = {"cell_name": "gate_off", "output": str(tmp_path / "off.gds")}
        if check_value is not None:
            options["check_placement"] = check_value
        report = compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "a", "generator_report": a},
                    {"id": "b", "generator_report": b},
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["a", "b"],
                    "origins_um": {
                        "a": {"x": 0.0, "y": 0.0},
                        "b": {"x": 9.0, "y": 2.0},
                    },
                },
                "options": options,
            }
        )
        assert (tmp_path / "off.gds").is_file()
        assert report["warnings"] == []


def test_compose_check_placement_edge_contact_passes(tmp_path, pdk_root):
    # Flush east abutment: shared edge, zero-width intersection -- the gate
    # passes and the output is written (edge contact is tolerated; only a
    # positive-area intersection fails).
    a = _gate_block(
        tmp_path,
        "a",
        {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        (1.0, 1.0, 9.0, 9.0),
    )
    b = _gate_block(
        tmp_path,
        "b",
        {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 3.0},
        (0.2, 0.2, 2.8, 2.8),
    )
    output = tmp_path / "gate_abut.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "a", "generator_report": a},
                {"id": "b", "generator_report": b},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["a", "b"],
                "origins_um": {
                    "a": {"x": 0.0, "y": 0.0},
                    "b": {"x": 10.0, "y": 0.0},
                },
            },
            "options": {
                "cell_name": "gate_abut",
                "output": str(output),
                "check_placement": True,
            },
        }
    )
    assert output.is_file()
    assert report["warnings"] == []


def test_compose_check_placement_abuts_cannot_exempt_overlap(tmp_path, pdk_root):
    # `abuts` suppresses the *clearance advisory* (#2638) -- it can never
    # exempt a positive-area intersection from the hard gate.
    a = _gate_block(
        tmp_path,
        "a",
        {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        (1.0, 1.0, 9.0, 9.0),
    )
    b = _gate_block(
        tmp_path,
        "b",
        {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 3.0},
        (0.2, 0.2, 2.8, 2.8),
    )
    output = tmp_path / "gate_abuts.gds"
    with pytest.raises(GenComposeError) as excinfo:
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "a", "generator_report": a, "abuts": ["b"]},
                    {"id": "b", "generator_report": b},
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["a", "b"],
                    "origins_um": {
                        "a": {"x": 0.0, "y": 0.0},
                        "b": {"x": 9.0, "y": 2.0},
                    },
                },
                "options": {
                    "cell_name": "gate_abuts",
                    "output": str(output),
                    "check_placement": True,
                },
            }
        )
    assert "(9.000, 2.000)-(10.000, 5.000)" in str(excinfo.value)
    assert not output.exists()


def test_compose_check_placement_isolated_growth_fails(tmp_path, pdk_root):
    # The #1679 isolated-growth fixture: "macro"'s declared bbox_um
    # (0,0)-(2,2) understates its real extent (-1,-1)-(0.8,3) (the stand-in
    # ring geometry), and "blockb" sits far east of both -- so the advisory
    # stays silent (the excess geometry overlaps nobody) but the opt-in gate
    # fails on the footprint growth alone.
    macro_gds = _write_gds_with_labeled_pad(
        tmp_path / "macro.gds", "macro_top", (0.2, 0.2, 0.8, 0.8), "CLK"
    )
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(macro_gds)
    top = layout.cell("macro_top")
    dbu = layout.dbu
    li1 = layout.layer(67, 20)

    def d(v):
        return int(round(v / dbu))

    top.shapes(li1).insert(kdb.Box(d(-1.0), d(-1.0), d(-0.5), d(3.0)))
    layout.write(macro_gds)

    blockb_gds = _write_gds_with_labeled_pad(
        tmp_path / "blockb.gds", "blockb_top", (0.0, 0.0, 0.6, 0.6), "DATA"
    )
    macro_report = {
        "generator": "fake_macro",
        "cell_name": "macro_top",
        "gds_path": macro_gds,
        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 2.0},
        "ports": [],
    }
    blockb_report = {
        "generator": "fake_blockb",
        "cell_name": "blockb_top",
        "gds_path": blockb_gds,
        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 0.6, "y1": 0.6},
        "ports": [],
    }
    output = tmp_path / "gate_growth.gds"

    def _request(check):
        return {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "macro", "generator_report": macro_report},
                {"id": "blockb", "generator_report": blockb_report},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["macro", "blockb"],
                "origins_um": {
                    "macro": {"x": 0.0, "y": 0.0},
                    "blockb": {"x": 10.0, "y": 0.0},
                },
            },
            "options": {
                "cell_name": "gate_growth",
                "output": str(output),
                "check_placement": check,
            },
        }

    # Option off: today's behavior -- success, no warnings (the excess
    # geometry overlaps nobody, so even the #1679 advisory is silent).
    report = compose(_request(False))
    assert output.is_file()
    assert report["warnings"] == []

    # Option on: the growth alone refuses the composition, naming the
    # block, both rectangles, and the sides that grew.
    with pytest.raises(GenComposeError) as excinfo:
        compose(_request(True))
    message = str(excinfo.value)
    assert "macro" in message
    assert "(-1.000, -1.000)-(0.800, 3.000)" in message  # measured
    assert "(0.000, 0.000)-(2.000, 2.000)" in message  # declared
    assert "grew" in message
    assert "left 1.000um" in message
    assert "bottom 1.000um" in message
    assert "top 1.000um" in message
    assert "blockb" not in message  # blockb's own declaration is honest


def test_compose_check_placement_rotated_external_cell_local_origin(tmp_path, pdk_root):
    # A `cell` block drawn at a nonzero local origin (#2367's case) and
    # placed rotate_180: both the declared and measured footprints land at
    # (-3,-2)-(-1,-1) in the composed frame, so growth is clean and the
    # pairwise intersection is reported in composed coordinates.
    io_gds = _write_gds_with_labeled_pad(
        tmp_path / "io.gds", "io_cell", (1.0, 1.0, 3.0, 2.0), "PAD"
    )
    other_gds = _write_gds_with_labeled_pad(
        tmp_path / "other.gds", "other_top", (0.0, 0.0, 1.0, 1.0), "D"
    )
    output = tmp_path / "gate_rot.gds"
    with pytest.raises(GenComposeError) as excinfo:
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {
                        "id": "io",
                        "orientation": "rotate_180",
                        "cell": {
                            "gds_path": io_gds,
                            "cell_name": "io_cell",
                            "bbox_um": {
                                "x0": 1.0,
                                "y0": 1.0,
                                "x1": 3.0,
                                "y1": 2.0,
                            },
                        },
                    },
                    {
                        "id": "other",
                        "cell": {"gds_path": other_gds, "cell_name": "other_top"},
                    },
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["io", "other"],
                    "origins_um": {
                        "io": {"x": 0.0, "y": 0.0},
                        "other": {"x": -2.5, "y": -1.5},
                    },
                },
                "options": {
                    "cell_name": "gate_rot",
                    "output": str(output),
                    "check_placement": True,
                },
            }
        )
    message = str(excinfo.value)
    assert "'io'" in message
    assert "'other'" in message
    # io's oriented footprint (-3,-2)-(-1,-1) vs other's placed
    # (-2.5,-1.5)-(-1.5,-0.5): intersection in the composed frame.
    assert "(-2.500, -1.500)-(-1.500, -1.000)" in message
    assert "grew" not in message  # oriented declaration matches measurement
    assert not output.exists()


def test_compose_check_placement_array_tile_growth_hidden_by_union(tmp_path, pdk_root):
    # Declared tile bbox (0,0)-(2,2); the real drawn geometry extends to
    # x=2.4 (0.4um of right growth). The 2x2 array's union (0,0)-(5,5)
    # still contains the grown base tile, so the #1679 advisory (which
    # compares the real extent against the placed *union*) stays silent --
    # the gate compares the base tile's own declaration, so it must fail.
    tile = _gate_block(
        tmp_path,
        "tile",
        {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 2.0},
        (0.2, 0.2, 2.4, 1.8),
    )
    output = tmp_path / "gate_array.gds"

    def _request(check):
        options = {"cell_name": "gate_array", "output": str(output)}
        if check is not None:
            options["check_placement"] = check
        return {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "tile", "generator_report": tile}],
            "placement": {
                "strategy": "array",
                "order": ["tile"],
                "rows": 2,
                "cols": 2,
                "row_pitch_um": 3.0,
                "col_pitch_um": 3.0,
            },
            "options": options,
        }

    report = compose(_request(False))
    assert output.is_file()
    assert report["warnings"] == []  # the union hides the tile growth

    with pytest.raises(GenComposeError) as excinfo:
        compose(_request(True))
    message = str(excinfo.value)
    assert "'tile'" in message
    assert "right 0.400um" in message
    assert "grew" in message


def test_compose_check_placement_must_be_a_boolean(tmp_path, pdk_root):
    tile = _gate_block(
        tmp_path,
        "tile",
        {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 2.0},
        (0.2, 0.2, 1.8, 1.8),
    )
    with pytest.raises(GenComposeError, match="check_placement must be a boolean"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "tile", "generator_report": tile}],
                "placement": {
                    "strategy": "row",
                    "order": ["tile"],
                    "spacing_um": 1.0,
                },
                "options": {
                    "cell_name": "gate_bool",
                    "output": str(tmp_path / "gate_bool.gds"),
                    "check_placement": "yes",
                },
            }
        )


def test_cli_gen_compose_check_placement_failure_exit_1_preserves_output(
    tmp_path, pdk_root, capsys
):
    # A strict failing request through the CLI: exit 1, the diagnostic
    # details in the JSON error envelope, and a pre-existing output file
    # preserved byte-for-byte (the gate fires before anything is written).
    a = _gate_block(
        tmp_path,
        "a",
        {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        (1.0, 1.0, 9.0, 9.0),
    )
    b = _gate_block(
        tmp_path,
        "b",
        {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 3.0},
        (0.2, 0.2, 2.8, 2.8),
    )
    output = tmp_path / "gate_cli.gds"
    output.write_text("pre-existing output sentinel")

    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "a", "generator_report": a},
                    {"id": "b", "generator_report": b},
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["a", "b"],
                    "origins_um": {
                        "a": {"x": 0.0, "y": 0.0},
                        "b": {"x": 9.0, "y": 2.0},
                    },
                },
                "options": {
                    "cell_name": "gate_cli",
                    "output": str(output),
                    "check_placement": True,
                },
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    message = error["error"]["message"]
    assert "'a'" in message
    assert "'b'" in message
    assert "(9.000, 2.000)-(10.000, 5.000)" in message
    assert output.read_text() == "pre-existing output sentinel"
