"""Tests for `klt gen-compose` and the `klayout_tools.gen_compose` library
module.

PDK resolution is exercised against a **fabricated** open_pdks-layout install
under ``tmp_path`` (mirrors `test_gen.py`/`test_pdk.py`) -- CI never
downloads a real PDK. The environment is scrubbed and the `pdk` module's
search-space constants are pointed away from the host by default (the
`_isolate` autouse fixture) so results are hermetic regardless of what is
installed on the machine running the suite.
"""

import json
from itertools import pairwise

import pytest

from klayout_tools import extract, gen, gen_compose, pdk
from klayout_tools.cli import main
from klayout_tools.decks import get_extraction_deck
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import (
    _ORIENTATION_KDB_ARGS,
    GenComposeError,
    _apply_orientation_um,
    _cleanup_points,
    _orient_bbox_um,
    _orient_port,
    _parse_array_placement,
    _parse_blocks,
    _pin_ref,
    _polyline_midpoint_um,
    _resolve_label_layer,
    _resolve_via_drop_layer,
    _translate_bbox,
    _union_bbox,
    array_placement_bbox_um,
    compose,
    compute_row_offsets,
    load_generator_report_arg,
    manhattan_backbone,
    resolve_explicit_offsets,
)


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant (just the layout probe)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Scrub PDK env vars and empty the host search space -- see test_pdk.py."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def both_pdk_root(tmp_path):
    """Both PDK families the phase-2 generators support -- mirrors
    `test_gen.py`'s own fixture of the same name, used by the stub-widen
    (#496) dual-deck DRC-clean tests below."""
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


def _gen_block(tmp_path, pdk_root, generator, cell_name, **params):
    """Run a real `klt gen` generator and return its response dict --
    building real `generator_report` fixtures the same way a caller would
    (rather than hand-writing a fake report, which risks drifting from the
    documented `klt gen` response shape)."""
    return _gen_block_variant(
        tmp_path, pdk_root, "sky130A", generator, cell_name, **params
    )


def _gen_block_variant(tmp_path, pdk_root, variant, generator, cell_name, **params):
    """Like :func:`_gen_block`, but for an arbitrary PDK ``variant`` (e.g.
    ``"gf180mcuD"``) -- used by the dual-deck stub-widen (#496) tests below."""
    output = tmp_path / f"{cell_name}.gds"
    request = {
        "schema": gen.REQUEST_SCHEMA,
        "generator": generator,
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "params": params,
        "options": {"cell_name": cell_name, "output": str(output)},
    }
    return gen.generate(request)


# --------------------------------------------------------------------------- #
# compute_row_offsets() -- pure placement math, no PDK/pya involvement
# --------------------------------------------------------------------------- #


def test_compute_row_offsets_single_block_is_degenerate():
    bboxes = {"a": {"x0": -0.5, "y0": -0.2, "x1": 3.0, "y1": 1.0}}
    offsets = compute_row_offsets(["a"], bboxes, spacing_um=1.0)
    assert offsets == {"a": {"x": 0.0, "y": 0.0}}


def test_compute_row_offsets_two_blocks_default_spacing():
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.0},
        "b": {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 1.0},
    }
    offsets = compute_row_offsets(["a", "b"], bboxes, spacing_um=1.0)
    assert offsets["a"] == {"x": 0.0, "y": 0.0}
    # b's bbox.x0 (0.0) must land at a's translated x1 (2.0) + spacing (1.0) = 3.0
    assert offsets["b"] == {"x": 3.0, "y": 0.0}


def test_compute_row_offsets_varying_spacing():
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.0},
        "b": {"x0": 0.0, "y0": 0.0, "x1": 3.0, "y1": 1.0},
    }
    for spacing in (0.0, 0.5, 2.5):
        offsets = compute_row_offsets(["a", "b"], bboxes, spacing_um=spacing)
        assert offsets["a"]["x"] == pytest.approx(0.0)
        assert offsets["b"]["x"] == pytest.approx(2.0 + spacing)


def test_compute_row_offsets_multi_block_ordering_and_negative_bbox():
    # A block whose own bbox extends into negative x (e.g. a guard-ringed
    # generator's bbox) must still end up exactly `spacing_um` past the
    # previous block's translated right edge.
    bboxes = {
        "ring": {"x0": -1.0, "y0": -1.0, "x1": 4.0, "y1": 3.0},
        "plain": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.0},
        "tail": {"x0": -0.5, "y0": 0.0, "x1": 1.5, "y1": 0.5},
    }
    order = ["ring", "plain", "tail"]
    offsets = compute_row_offsets(order, bboxes, spacing_um=1.0)

    assert offsets["ring"] == {"x": 0.0, "y": 0.0}

    # translated x1 of "ring" is 4.0 -> "plain".x0 (0.0) + offset must be 5.0
    assert offsets["plain"]["x"] == pytest.approx(5.0)
    plain_x1 = bboxes["plain"]["x1"] + offsets["plain"]["x"]
    assert plain_x1 == pytest.approx(7.0)

    # "tail".x0 (-0.5) + offset must land at plain_x1 + spacing (8.0)
    assert offsets["tail"]["x"] == pytest.approx(8.0 - (-0.5))
    tail_x0 = bboxes["tail"]["x0"] + offsets["tail"]["x"]
    assert tail_x0 == pytest.approx(8.0)

    # every block keeps y unchanged (row placement translates x only)
    for block_id in order:
        assert offsets[block_id]["y"] == 0.0


def test_compute_row_offsets_reorders_by_order_not_dict_iteration():
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
        "b": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
    }
    offsets_ba = compute_row_offsets(["b", "a"], bboxes, spacing_um=0.0)
    assert offsets_ba["b"] == {"x": 0.0, "y": 0.0}
    assert offsets_ba["a"] == {"x": 1.0, "y": 0.0}


# --------------------------------------------------------------------------- #
# resolve_explicit_offsets() -- pure placement math, no PDK/pya involvement
# (#321, mirrors the compute_row_offsets() suite above)
# --------------------------------------------------------------------------- #


def test_resolve_explicit_offsets_single_block_is_degenerate():
    origins = {"a": {"x": 3.5, "y": -2.0}}
    offsets = resolve_explicit_offsets(["a"], origins)
    assert offsets == {"a": {"x": 3.5, "y": -2.0}}


def test_resolve_explicit_offsets_multi_block_negative_and_positive_origins():
    # Unlike compute_row_offsets, a block's own bbox_um plays no role at all
    # -- origins_um IS offset_um, verbatim, per block id.
    origins = {
        "ring": {"x": 0.0, "y": 0.0},
        "plain": {"x": -10.0, "y": 15.0},
        "tail": {"x": 60.0, "y": 20.0},
    }
    order = ["ring", "plain", "tail"]
    offsets = resolve_explicit_offsets(order, origins)
    assert offsets["ring"] == {"x": 0.0, "y": 0.0}
    assert offsets["plain"] == {"x": -10.0, "y": 15.0}
    assert offsets["tail"] == {"x": 60.0, "y": 20.0}


def test_resolve_explicit_offsets_reorders_by_order_not_dict_iteration():
    origins = {
        "a": {"x": 1.0, "y": 2.0},
        "b": {"x": 3.0, "y": 4.0},
    }
    offsets_ba = resolve_explicit_offsets(["b", "a"], origins)
    assert offsets_ba["b"] == {"x": 3.0, "y": 4.0}
    assert offsets_ba["a"] == {"x": 1.0, "y": 2.0}


# --------------------------------------------------------------------------- #
# array_placement_bbox_um() -- pure placement math, no PDK/pya involvement
# (#1053, mirrors the compute_row_offsets/resolve_explicit_offsets suites
# above)
# --------------------------------------------------------------------------- #


def test_array_placement_bbox_um_single_tile_is_degenerate():
    # rows=cols=1 degenerates to a plain translation by origin_um -- the
    # pitches play no role when there is only one tile on each axis.
    bbox = {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.0}
    array_params = {
        "rows": 1,
        "cols": 1,
        "row_pitch_um": 5.0,
        "col_pitch_um": 5.0,
        "origin_um": {"x": 10.0, "y": -3.0},
    }
    bbox_um = array_placement_bbox_um(bbox, array_params)
    assert bbox_um == {"x0": 10.0, "y0": -3.0, "x1": 12.0, "y1": -2.0}


def test_array_placement_bbox_um_grows_cols_along_x_rows_along_y():
    bbox = {"x0": 0.0, "y0": 0.0, "x1": 9.26, "y1": 0.42}
    array_params = {
        "rows": 2,
        "cols": 3,
        "row_pitch_um": 5.0,
        "col_pitch_um": 15.0,
        "origin_um": {"x": 1.0, "y": 2.0},
    }
    bbox_um = array_placement_bbox_um(bbox, array_params)
    assert bbox_um == pytest.approx({"x0": 1.0, "y0": 2.0, "x1": 40.26, "y1": 7.42})


def test_array_placement_bbox_um_default_zero_origin():
    bbox = {"x0": -1.0, "y0": -1.0, "x1": 1.0, "y1": 1.0}
    array_params = {
        "rows": 4,
        "cols": 1,
        "row_pitch_um": 2.0,
        "col_pitch_um": 2.0,
        "origin_um": {"x": 0.0, "y": 0.0},
    }
    bbox_um = array_placement_bbox_um(bbox, array_params)
    # 4 rows, 1 col: grows along y only -- (rows - 1) * row_pitch_um = 6.0
    assert bbox_um == {"x0": -1.0, "y0": -1.0, "x1": 1.0, "y1": 7.0}


# --------------------------------------------------------------------------- #
# _parse_array_placement() -- request-shape validation for "array" (#1053)
# --------------------------------------------------------------------------- #


def test_parse_array_placement_valid_full_request():
    params = _parse_array_placement(
        {
            "strategy": "array",
            "rows": 16,
            "cols": 8,
            "row_pitch_um": 5.0,
            "col_pitch_um": 3.0,
            "origin_um": {"x": 1.5, "y": -2.5},
        }
    )
    assert params == {
        "rows": 16,
        "cols": 8,
        "row_pitch_um": 5.0,
        "col_pitch_um": 3.0,
        "origin_um": {"x": 1.5, "y": -2.5},
    }


def test_parse_array_placement_default_origin_is_zero_zero():
    params = _parse_array_placement(
        {"rows": 1, "cols": 1, "row_pitch_um": 1.0, "col_pitch_um": 1.0}
    )
    assert params["origin_um"] == {"x": 0.0, "y": 0.0}


def test_parse_array_placement_allows_degenerate_1x1():
    params = _parse_array_placement(
        {"rows": 1, "cols": 1, "row_pitch_um": 1.0, "col_pitch_um": 1.0}
    )
    assert params["rows"] == 1
    assert params["cols"] == 1


def test_parse_array_placement_allows_non_square_rows_cols():
    params = _parse_array_placement(
        {"rows": 7, "cols": 2, "row_pitch_um": 1.0, "col_pitch_um": 1.0}
    )
    assert params["rows"] == 7
    assert params["cols"] == 2


@pytest.mark.parametrize("bad_rows", [0, -1, 1.5, "3", True, None])
def test_parse_array_placement_rejects_bad_rows(bad_rows):
    with pytest.raises(GenComposeError, match="rows"):
        _parse_array_placement(
            {"rows": bad_rows, "cols": 2, "row_pitch_um": 1.0, "col_pitch_um": 1.0}
        )


@pytest.mark.parametrize("bad_cols", [0, -1, 1.5, "3", True, None])
def test_parse_array_placement_rejects_bad_cols(bad_cols):
    with pytest.raises(GenComposeError, match="cols"):
        _parse_array_placement(
            {"rows": 2, "cols": bad_cols, "row_pitch_um": 1.0, "col_pitch_um": 1.0}
        )


@pytest.mark.parametrize("bad_pitch", [0, 0.0, -1.0, "1.0", True, None])
def test_parse_array_placement_rejects_zero_or_negative_row_pitch(bad_pitch):
    with pytest.raises(GenComposeError, match="row_pitch_um"):
        _parse_array_placement(
            {"rows": 2, "cols": 2, "row_pitch_um": bad_pitch, "col_pitch_um": 1.0}
        )


@pytest.mark.parametrize("bad_pitch", [0, 0.0, -1.0, "1.0", True, None])
def test_parse_array_placement_rejects_zero_or_negative_col_pitch(bad_pitch):
    with pytest.raises(GenComposeError, match="col_pitch_um"):
        _parse_array_placement(
            {"rows": 2, "cols": 2, "row_pitch_um": 1.0, "col_pitch_um": bad_pitch}
        )


def test_parse_array_placement_rejects_non_numeric_origin_fields():
    with pytest.raises(GenComposeError, match="origin_um"):
        _parse_array_placement(
            {
                "rows": 2,
                "cols": 2,
                "row_pitch_um": 1.0,
                "col_pitch_um": 1.0,
                "origin_um": {"x": "0.0", "y": 0.0},
            }
        )


def test_parse_array_placement_rejects_non_object_origin():
    with pytest.raises(GenComposeError, match="origin_um"):
        _parse_array_placement(
            {
                "rows": 2,
                "cols": 2,
                "row_pitch_um": 1.0,
                "col_pitch_um": 1.0,
                "origin_um": [0.0, 0.0],
            }
        )


# --------------------------------------------------------------------------- #
# _apply_orientation_um()/_orient_bbox_um()/_orient_port() -- block
# orientation (mirror/rotate) transform math, #1166
# --------------------------------------------------------------------------- #


def test_apply_orientation_um_none_is_identity():
    assert _apply_orientation_um(10.0, 3.0, "none") == (10.0, 3.0)


def test_apply_orientation_um_mirror_x_negates_x_only():
    assert _apply_orientation_um(10.0, 3.0, "mirror_x") == (-10.0, 3.0)


def test_apply_orientation_um_mirror_y_negates_y_only():
    assert _apply_orientation_um(10.0, 3.0, "mirror_y") == (10.0, -3.0)


def test_apply_orientation_um_rotate_180_negates_both():
    assert _apply_orientation_um(10.0, 3.0, "rotate_180") == (-10.0, -3.0)


@pytest.mark.parametrize("orientation", ["none", "mirror_x", "mirror_y", "rotate_180"])
def test_orientation_kdb_trans_matches_apply_orientation_um(orientation):
    # The kdb.Trans(rot, mirrx, x, y) construction _write_composed_gds/
    # read_block_layer_geometry use for the actual drawn geometry must
    # transform a point identically to _apply_orientation_um's own math --
    # otherwise a block's reported metadata (bbox_um/ports[]) would disagree
    # with what is actually drawn (the core correctness requirement #1166's
    # acceptance criteria calls out).
    import klayout.db as kdb

    rot, mirrx = _ORIENTATION_KDB_ARGS[orientation]
    trans = kdb.Trans(rot, mirrx, 0, 0)
    px, py = 1200, 700  # arbitrary dbu-space point
    got = trans * kdb.Point(px, py)
    want_x, want_y = _apply_orientation_um(px, py, orientation)
    assert (got.x, got.y) == (want_x, want_y)


def test_orient_bbox_um_none_is_unchanged():
    bbox = {"x0": -1.0, "y0": -2.0, "x1": 5.0, "y1": 3.0}
    assert _orient_bbox_um(bbox, "none") == bbox


def test_orient_bbox_um_mirror_x_negates_x_and_resorts():
    bbox = {"x0": -1.0, "y0": -2.0, "x1": 5.0, "y1": 3.0}
    oriented = _orient_bbox_um(bbox, "mirror_x")
    assert oriented == {"x0": -5.0, "y0": -2.0, "x1": 1.0, "y1": 3.0}
    # width/height are invariant under a mirror.
    assert oriented["x1"] - oriented["x0"] == pytest.approx(bbox["x1"] - bbox["x0"])
    assert oriented["y1"] - oriented["y0"] == pytest.approx(bbox["y1"] - bbox["y0"])


def test_orient_bbox_um_mirror_y_negates_y_and_resorts():
    bbox = {"x0": -1.0, "y0": -2.0, "x1": 5.0, "y1": 3.0}
    oriented = _orient_bbox_um(bbox, "mirror_y")
    assert oriented == {"x0": -1.0, "y0": -3.0, "x1": 5.0, "y1": 2.0}


def test_orient_bbox_um_rotate_180_negates_both_and_resorts():
    bbox = {"x0": -1.0, "y0": -2.0, "x1": 5.0, "y1": 3.0}
    oriented = _orient_bbox_um(bbox, "rotate_180")
    assert oriented == {"x0": -5.0, "y0": -3.0, "x1": 1.0, "y1": 2.0}


def test_orient_port_mirror_x_negates_x_and_flips_east_west_direction():
    port = {"name": "D", "x_um": 3.0, "y_um": 1.5, "direction_deg": 0, "width_um": 0.22}
    oriented = _orient_port(port, "mirror_x")
    assert oriented["x_um"] == -3.0
    assert oriented["y_um"] == 1.5
    assert oriented["direction_deg"] == 180
    # Identity/contact-size fields are untouched.
    assert oriented["name"] == "D"
    assert oriented["width_um"] == 0.22


def test_orient_port_mirror_y_flips_north_south_direction_only():
    port = {"name": "G", "x_um": 3.0, "y_um": 1.5, "direction_deg": 90}
    oriented = _orient_port(port, "mirror_y")
    assert oriented["x_um"] == 3.0
    assert oriented["y_um"] == -1.5
    assert oriented["direction_deg"] == 270


def test_orient_port_leaves_missing_geometry_untouched():
    # A port with no x_um/y_um (klt draw's own ports[] can omit geometry
    # entirely) must not raise or fabricate a position.
    port = {"name": "N1"}
    assert _orient_port(port, "mirror_x") == {"name": "N1"}


# --------------------------------------------------------------------------- #
# load_generator_report_arg() -- path-or-inline duality
# --------------------------------------------------------------------------- #


def test_load_generator_report_arg_inline_dict_passthrough():
    report = {"generator": "resistor_strip", "cell_name": "x", "gds_path": "x.gds"}
    assert load_generator_report_arg(report) is report


def test_load_generator_report_arg_reads_file(tmp_path):
    report = {"generator": "resistor_strip", "cell_name": "x", "gds_path": "x.gds"}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    assert load_generator_report_arg(str(path)) == report


def test_load_generator_report_arg_missing_file_raises():
    with pytest.raises(GenComposeError, match="not found"):
        load_generator_report_arg("/nonexistent/report.json")


def test_load_generator_report_arg_rejects_non_dict_non_str():
    with pytest.raises(GenComposeError):
        load_generator_report_arg(42)


def test_load_generator_report_arg_resolves_relative_to_request_dir(tmp_path):
    # A relative path resolves against request_dir (#328), not the process cwd
    # -- mirrors klt lvs's load_request_arg/_resolve_relative convention.
    report = {"generator": "resistor_strip", "cell_name": "x", "gds_path": "x.gds"}
    request_dir = tmp_path / "some" / "dir"
    request_dir.mkdir(parents=True)
    (request_dir / "report.json").write_text(json.dumps(report))

    assert load_generator_report_arg("report.json", str(request_dir)) == report


def test_load_generator_report_arg_defaults_to_cwd_when_no_request_dir(
    tmp_path, monkeypatch
):
    # request_dir omitted (None) -- backward compat with a caller that has no
    # request file at all: resolve against the process's own cwd, unchanged.
    report = {"generator": "resistor_strip", "cell_name": "x", "gds_path": "x.gds"}
    (tmp_path / "report.json").write_text(json.dumps(report))
    monkeypatch.chdir(tmp_path)

    assert load_generator_report_arg("report.json") == report


def test_load_generator_report_arg_absolute_path_unaffected_by_request_dir(tmp_path):
    report = {"generator": "resistor_strip", "cell_name": "x", "gds_path": "x.gds"}
    path = tmp_path / "abs_report.json"
    path.write_text(json.dumps(report))
    other_dir = tmp_path / "unrelated"
    other_dir.mkdir()

    assert load_generator_report_arg(str(path), str(other_dir)) == report


# --------------------------------------------------------------------------- #
# _parse_blocks() -- blocks[].orientation (#1166)
# --------------------------------------------------------------------------- #


def _fake_mos_block_report():
    """A hand-built report shaped like `mos_array`'s own (rows=1, cols=1) --
    enough to exercise blocks[].orientation parsing without a PDK/klt gen
    call: a bbox and S (left, 180deg)/D (right, 0deg)/G (top, 90deg) ports."""
    return {
        "generator": "mos_array",
        "cell_name": "m0",
        "gds_path": "m0.gds",
        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.0},
        "ports": [
            {
                "name": "U0_S",
                "x_um": 0.1,
                "y_um": 0.5,
                "direction_deg": 180,
                "width_um": 0.22,
                "layer": {"layer": 68, "datatype": 20},
            },
            {
                "name": "U0_D",
                "x_um": 1.9,
                "y_um": 0.5,
                "direction_deg": 0,
                "width_um": 0.22,
                "layer": {"layer": 68, "datatype": 20},
            },
            {
                "name": "U0_G",
                "x_um": 1.0,
                "y_um": 0.9,
                "direction_deg": 90,
                "width_um": 0.15,
                "layer": {"layer": 66, "datatype": 20},
            },
        ],
    }


def test_parse_blocks_defaults_orientation_to_none_unchanged():
    blocks = _parse_blocks([{"id": "a", "generator_report": _fake_mos_block_report()}])
    block = blocks["a"]
    assert block["orientation"] == "none"
    assert block["bbox_um"] == {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.0}
    assert block["ports"]["U0_D"]["x_um"] == 1.9
    assert block["ports"]["U0_D"]["direction_deg"] == 0


def test_parse_blocks_mirror_x_transforms_bbox_and_ports_consistently():
    blocks = _parse_blocks(
        [
            {
                "id": "a",
                "generator_report": _fake_mos_block_report(),
                "orientation": "mirror_x",
            }
        ]
    )
    block = blocks["a"]
    assert block["orientation"] == "mirror_x"
    # bbox: x negated and re-sorted, width unchanged, y untouched.
    assert block["bbox_um"] == {"x0": -2.0, "y0": 0.0, "x1": 0.0, "y1": 1.0}
    # U0_D (was on the right edge, facing +x/0deg) now sits on the left
    # (negated x) and faces -x/180deg -- the exact transform that lets it
    # face a same-facing neighbour's own drain (#1164's root cause #1).
    assert block["ports"]["U0_D"]["x_um"] == -1.9
    assert block["ports"]["U0_D"]["y_um"] == 0.5
    assert block["ports"]["U0_D"]["direction_deg"] == 180
    # U0_S mirrors symmetrically to the right, now facing +x.
    assert block["ports"]["U0_S"]["x_um"] == -0.1
    assert block["ports"]["U0_S"]["direction_deg"] == 0
    # U0_G's direction (90, north) is unaffected by a left-right mirror.
    assert block["ports"]["U0_G"]["x_um"] == -1.0
    assert block["ports"]["U0_G"]["direction_deg"] == 90
    # Non-geometric fields (name, width_um, layer) are untouched.
    assert block["ports"]["U0_D"]["width_um"] == 0.22
    assert block["ports"]["U0_D"]["layer"] == {"layer": 68, "datatype": 20}


def test_parse_blocks_orientation_is_per_block_independent():
    blocks = _parse_blocks(
        [
            {"id": "a", "generator_report": _fake_mos_block_report()},
            {
                "id": "b",
                "generator_report": _fake_mos_block_report(),
                "orientation": "mirror_x",
            },
        ]
    )
    assert blocks["a"]["orientation"] == "none"
    assert blocks["a"]["ports"]["U0_D"]["x_um"] == 1.9
    assert blocks["b"]["orientation"] == "mirror_x"
    assert blocks["b"]["ports"]["U0_D"]["x_um"] == -1.9


def test_parse_blocks_rejects_unsupported_orientation():
    with pytest.raises(GenComposeError, match="orientation"):
        _parse_blocks(
            [
                {
                    "id": "a",
                    "generator_report": _fake_mos_block_report(),
                    "orientation": "flip",
                }
            ]
        )


# --------------------------------------------------------------------------- #
# compose() -- request-shape validation
# --------------------------------------------------------------------------- #


def test_compose_rejects_empty_blocks(pdk_root):
    with pytest.raises(GenComposeError, match="blocks"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [],
                "placement": {"strategy": "row", "order": [], "spacing_um": 1.0},
            }
        )


def test_compose_rejects_unsupported_placement_strategy(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="strategy"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "grid", "order": ["r"], "spacing_um": 1.0},
            }
        )


def test_compose_rejects_order_not_matching_blocks(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="order"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {
                    "strategy": "row",
                    "order": ["r", "missing"],
                    "spacing_um": 1.0,
                },
            }
        )


def test_compose_rejects_negative_spacing(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="spacing_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": -1.0},
            }
        )


# --------------------------------------------------------------------------- #
# compose() -- request.pdk unknown-key validation (#328)
# --------------------------------------------------------------------------- #


def test_compose_rejects_unknown_pdk_key_name_typo(tmp_path, pdk_root):
    # {"pdk": {"name": ...}} is a plausible typo for "variant" (klt gen's own
    # response calls this field "name") -- must be an application error, not
    # a silent fallback to a different resolved PDK variant.
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="name"):
        compose(
            {
                "pdk": {"name": "gf180mcuD"},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            }
        )


def test_compose_rejects_unknown_pdk_key_alongside_valid_ones(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="bogus"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root), "bogus": 1},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            }
        )


def test_compose_accepts_pdk_variant_and_root(tmp_path, pdk_root):
    # Regression guard: the documented {"pdk": {"variant": ..., "root": ...}}
    # shape must keep working unaffected by the new allow-list check.
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    output = tmp_path / "valid_pdk.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "r", "generator_report": block}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {"cell_name": "valid_pdk_0", "output": str(output)},
        }
    )
    assert report["pdk"]["variant"] == "sky130A"


def test_compose_accepts_empty_pdk_object(tmp_path, pdk_root, monkeypatch):
    # Regression guard: an absent pdk key, and an explicitly empty {}, must
    # both keep resolving via find_pdk()'s own $PDK/default fallback exactly
    # as before -- the allow-list check must not reject an empty dict.
    monkeypatch.setenv("PDK_ROOT", str(pdk_root))
    monkeypatch.setenv("PDK", "sky130A")
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    output = tmp_path / "empty_pdk.gds"
    report = compose(
        {
            "pdk": {},
            "blocks": [{"id": "r", "generator_report": block}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {"cell_name": "empty_pdk_0", "output": str(output)},
        }
    )
    assert report["pdk"]["variant"] == "sky130A"


def test_compose_accepts_absent_pdk_key(tmp_path, pdk_root, monkeypatch):
    monkeypatch.setenv("PDK_ROOT", str(pdk_root))
    monkeypatch.setenv("PDK", "sky130A")
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    output = tmp_path / "no_pdk_key.gds"
    report = compose(
        {
            "blocks": [{"id": "r", "generator_report": block}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {"cell_name": "no_pdk_key_0", "output": str(output)},
        }
    )
    assert report["pdk"]["variant"] == "sky130A"


# --------------------------------------------------------------------------- #
# compose() -- request_dir threading for blocks[].generator_report (#328)
# --------------------------------------------------------------------------- #


def test_compose_generator_report_path_resolves_against_request_dir(
    tmp_path, pdk_root, monkeypatch
):
    # A relative generator_report path resolves against request_dir, not the
    # process cwd -- confirm by running compose() from an unrelated cwd.
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    report_path = request_dir / "r0.json"
    report_path.write_text(json.dumps(block))

    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    output = tmp_path / "request_dir_relative.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "r", "generator_report": "r0.json"}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {"cell_name": "request_dir_relative", "output": str(output)},
        },
        request_dir=str(request_dir),
    )
    assert output.is_file()
    assert report["blocks"][0]["id"] == "r"


def test_compose_generator_report_path_resolves_against_cwd_without_request_dir(
    tmp_path, pdk_root, monkeypatch
):
    # No request_dir given (None) -- backward compat: resolve a relative
    # generator_report against the process's own cwd, as before #328
    # (test_metrics_regression.py's existing compose() call site relies on
    # this).
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    report_path = tmp_path / "r0.json"
    report_path.write_text(json.dumps(block))
    monkeypatch.chdir(tmp_path)

    output = tmp_path / "cwd_relative.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "r", "generator_report": "r0.json"}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {"cell_name": "cwd_relative", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["blocks"][0]["id"] == "r"


def test_compose_generator_report_absolute_path_unaffected_by_request_dir(
    tmp_path, pdk_root
):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    report_path = tmp_path / "abs_r0.json"
    report_path.write_text(json.dumps(block))

    other_dir = tmp_path / "unrelated_request_dir"
    other_dir.mkdir()

    output = tmp_path / "abs_generator_report.gds"
    compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "r", "generator_report": str(report_path)}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {"cell_name": "abs_generator_report_0", "output": str(output)},
        },
        request_dir=str(other_dir),
    )
    assert output.is_file()


def test_compose_generator_report_inline_object_unaffected_by_request_dir(
    tmp_path, pdk_root
):
    # generator_report given inline (an object, not a path string) never
    # touches the filesystem at all -- request_dir must have no effect on it.
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    nonexistent_dir = str(tmp_path / "does_not_exist")

    output = tmp_path / "inline_generator_report.gds"
    compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "r", "generator_report": block}],
            "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            "options": {
                "cell_name": "inline_generator_report_0",
                "output": str(output),
            },
        },
        request_dir=nonexistent_dir,
    )
    assert output.is_file()


# --------------------------------------------------------------------------- #
# compose() -- "explicit" placement.strategy request-shape validation (#321)
# --------------------------------------------------------------------------- #


def test_compose_explicit_rejects_missing_origin_for_order_id(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    with pytest.raises(GenComposeError, match="origins_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": r1},
                    {"id": "b2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["b1", "b2"],
                    "origins_um": {"b1": {"x": 0.0, "y": 0.0}},
                },
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_explicit_rejects_origin_for_id_not_in_order(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    with pytest.raises(GenComposeError, match="origins_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": {
                    "strategy": "explicit",
                    "order": ["b1"],
                    "origins_um": {
                        "b1": {"x": 0.0, "y": 0.0},
                        "unknown": {"x": 1.0, "y": 1.0},
                    },
                },
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_explicit_rejects_non_numeric_origin_fields(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    with pytest.raises(GenComposeError, match="origins_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": {
                    "strategy": "explicit",
                    "order": ["b1"],
                    "origins_um": {"b1": {"x": "0.0", "y": 0.0}},
                },
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_explicit_rejects_missing_origins_um_entirely(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    with pytest.raises(GenComposeError, match="origins_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": {"strategy": "explicit", "order": ["b1"]},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_explicit_ignores_spacing_um_when_present(tmp_path, pdk_root):
    # placement.spacing_um alongside strategy: "explicit" must not error --
    # it is simply unused (Acceptance Criteria / Test Plan edge case).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "explicit_with_spacing.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "b1", "generator_report": r1}],
            "placement": {
                "strategy": "explicit",
                "order": ["b1"],
                "origins_um": {"b1": {"x": 5.0, "y": -3.0}},
                "spacing_um": 999.0,
            },
            "options": {"cell_name": "explicit_0", "output": str(output)},
        }
    )
    assert report["blocks"][0]["offset_um"] == {"x": 5.0, "y": -3.0}


def test_compose_explicit_allows_overlapping_origins(tmp_path, pdk_root):
    # Overlapping/abutting explicit origins are not validated by gen-compose
    # itself -- geometry is advisory, klt drc is the rule-compliance
    # authority (Acceptance Criteria). Composing two blocks at the identical
    # origin must succeed, not raise.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "overlap.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["b1", "b2"],
                "origins_um": {
                    "b1": {"x": 0.0, "y": 0.0},
                    "b2": {"x": 0.0, "y": 0.0},
                },
            },
            "options": {"cell_name": "overlap_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["blocks"][0]["offset_um"] == {"x": 0.0, "y": 0.0}
    assert report["blocks"][1]["offset_um"] == {"x": 0.0, "y": 0.0}


def test_compose_explicit_warns_on_insufficient_clearance(tmp_path, pdk_root):
    # #692: b1 declares a real min_spacing_um (via generator_report drc_hints,
    # resistor_strip's own spacing_um param) but is placed with 0um clearance
    # from b2 under strategy: "explicit" -- gen-compose must not raise (still
    # advisory), but must add a warning naming both blocks so a caller doesn't
    # have to discover the resulting same-layer merge later via `klt
    # extract`'s `merged_net_labels` diagnostic.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1", spacing_um=1.0)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2", spacing_um=0.0)
    b1_x1 = r1["bbox_um"]["x1"]
    output = tmp_path / "explicit_flush.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["b1", "b2"],
                "origins_um": {
                    "b1": {"x": 0.0, "y": 0.0},
                    # Touching b1's right edge exactly -- 0um clearance.
                    "b2": {"x": b1_x1, "y": 0.0},
                },
            },
            "options": {"cell_name": "explicit_flush_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert len(report["warnings"]) == 1
    warning = report["warnings"][0]
    assert "b1" in warning
    assert "b2" in warning
    assert "min_spacing_um" in warning
    assert "explicit" in warning
    assert "0.00um" in warning  # actual clearance found
    assert "1.00um" in warning  # b1's declared min_spacing_um


def test_compose_explicit_no_warning_when_clearance_sufficient(tmp_path, pdk_root):
    # Companion to the insufficient-clearance case above: the same declared
    # min_spacing_um, but placed far enough apart that no warning fires.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1", spacing_um=1.0)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2", spacing_um=0.0)
    b1_x1 = r1["bbox_um"]["x1"]
    output = tmp_path / "explicit_clear.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["b1", "b2"],
                "origins_um": {
                    "b1": {"x": 0.0, "y": 0.0},
                    # 2um clearance -- more than b1's declared 1.0um minimum.
                    "b2": {"x": b1_x1 + 2.0, "y": 0.0},
                },
            },
            "options": {"cell_name": "explicit_clear_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["warnings"] == []


def test_compose_explicit_no_warning_when_neither_block_declares_min_spacing(
    tmp_path, pdk_root
):
    # Today's common/default case (Acceptance Criteria (c)): neither block
    # declares a min_spacing_um (resistor_strip's spacing_um param set to 0),
    # so even a fully overlapping placement gets no new warning.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1", spacing_um=0.0)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2", spacing_um=0.0)
    output = tmp_path / "explicit_no_hint.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["b1", "b2"],
                "origins_um": {
                    "b1": {"x": 0.0, "y": 0.0},
                    "b2": {"x": 0.0, "y": 0.0},
                },
            },
            "options": {"cell_name": "explicit_no_hint_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["warnings"] == []


def test_compose_row_strategy_has_no_clearance_warnings(tmp_path, pdk_root):
    # #692's clearance advisory is scoped to strategy: "explicit" only --
    # "row" placement must never gain a new warning from it, even when a
    # block's own declared min_spacing_um exceeds the row's own spacing_um
    # (here 0.0, so blocks are placed touching -- 0um clearance).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1", spacing_um=5.0)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2", spacing_um=5.0)
    output = tmp_path / "row_no_warning.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2"],
                "spacing_um": 0.0,
            },
            "options": {"cell_name": "row_no_warning_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["warnings"] == []


def test_compose_explicit_places_three_blocks_at_non_collinear_origins(
    tmp_path, pdk_root
):
    # Integration test: an L-shaped floorplan (non-collinear (x, y) origins)
    # -- confirms the composed bbox_um is the union of each block's own bbox
    # translated by its declared origin, not a computed row.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")  # bbox: (0,0)-(~2, w)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    r3 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r3")

    origins = {
        "a": {"x": 0.0, "y": 0.0},
        "b": {"x": 0.0, "y": 50.0},
        "c": {"x": 50.0, "y": 25.0},
    }
    output = tmp_path / "l_shape.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "a", "generator_report": r1},
                {"id": "b", "generator_report": r2},
                {"id": "c", "generator_report": r3},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["a", "b", "c"],
                "origins_um": origins,
            },
            "options": {"cell_name": "l_shape_0", "output": str(output)},
        }
    )
    assert output.is_file()
    for block_id in ("a", "b", "c"):
        entry = next(b for b in report["blocks"] if b["id"] == block_id)
        assert entry["offset_um"] == origins[block_id]

    expected_bbox = _union_bbox(
        [
            _translate_bbox(r["bbox_um"], origins[block_id])
            for block_id, r in (("a", r1), ("b", r2), ("c", r3))
        ]
    )
    assert report["bbox_um"] == pytest.approx(expected_bbox)

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    assert layout.cell("l_shape_0") is not None


def test_compose_explicit_routes_net_with_vertical_jog(tmp_path, pdk_root):
    # Acceptance Criteria: a connectivity[] net between two blocks placed at
    # explicit, non-collinear (x, y) positions routes correctly through the
    # existing manhattan_backbone/route_two_pin path, including a case where
    # the jog direction is *vertical* rather than horizontal. resistor_strip's
    # P2 (east-facing, direction_deg=0) and P1 (west-facing, direction_deg=180)
    # are both x-facing, so placing b2 to b1's east *and* north forces
    # manhattan_backbone's "both horizontal" branch to draw a vertical jog
    # (see manhattan_backbone's docstring/test_manhattan_backbone_z_jog_...).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")

    b1_x1 = r1["bbox_um"]["x1"]
    # Enough horizontal channel for the routing width (0.17um) plus a large
    # vertical offset so the jog is unambiguously vertical, not a straight
    # horizontal span.
    origins_um = {
        "b1": {"x": 0.0, "y": 0.0},
        "b2": {"x": b1_x1 + 3.0, "y": 20.0},
    }
    output = tmp_path / "vertical_jog.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["b1", "b2"],
                "origins_um": origins_um,
            },
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "vjog_0", "output": str(output)},
        }
    )
    assert output.is_file()
    net = report["nets"][0]
    assert net["routed"] is True
    assert report["unrouted_nets"] == []

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("vjog_0")
    li1 = layout.layer(67, 20)
    paths = [s for s in top.shapes(li1).each() if s.is_path()]
    assert len(paths) == 1
    points = [
        (pt.x * layout.dbu, pt.y * layout.dbu) for pt in paths[0].path.each_point()
    ]
    # A vertical jog means at least one interior segment shares an x with its
    # neighbour but differs in y (as opposed to a purely horizontal span).
    has_vertical_segment = any(
        abs(p0[0] - p1[0]) < 1e-6 and abs(p0[1] - p1[1]) > 1e-6
        for p0, p1 in zip(points, points[1:], strict=False)
    )
    assert has_vertical_segment


# --------------------------------------------------------------------------- #
# compose() -- "array" placement.strategy request-shape validation (#1053)
# --------------------------------------------------------------------------- #


def test_compose_array_rejects_more_than_one_block(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    with pytest.raises(GenComposeError, match="exactly one blocks\\[\\] entry"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": r1},
                    {"id": "b2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "array",
                    "order": ["b1", "b2"],
                    "rows": 2,
                    "cols": 2,
                    "row_pitch_um": 5.0,
                    "col_pitch_um": 5.0,
                },
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", 0),
        ("rows", -1),
        ("cols", 0),
        ("cols", -1),
        ("row_pitch_um", 0.0),
        ("row_pitch_um", -1.0),
        ("col_pitch_um", 0.0),
        ("col_pitch_um", -1.0),
    ],
)
def test_compose_array_rejects_malformed_grid_fields(tmp_path, pdk_root, field, value):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    placement = {
        "strategy": "array",
        "order": ["b1"],
        "rows": 2,
        "cols": 2,
        "row_pitch_um": 5.0,
        "col_pitch_um": 5.0,
    }
    placement[field] = value
    with pytest.raises(GenComposeError):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": placement,
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_array_rejects_missing_required_grid_field(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    with pytest.raises(GenComposeError, match="row_pitch_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": {
                    "strategy": "array",
                    "order": ["b1"],
                    "rows": 2,
                    "cols": 2,
                    "col_pitch_um": 5.0,
                },
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


# --------------------------------------------------------------------------- #
# compose() -- "array" placement.strategy integration tests (#1053)
# --------------------------------------------------------------------------- #


def test_compose_array_places_2x3_grid_as_single_hierarchical_instance(
    tmp_path, pdk_root
):
    # Acceptance criteria: the composed GDS must emit exactly ONE
    # kdb.CellInstArray hierarchical instance for a rows*cols tiling, not
    # rows*cols separate inserts -- verified by inspecting the output GDS's
    # own instance count (each_inst()), not just rendered geometry (a
    # flattened rows*cols-insert implementation could look identical).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "array_2x3.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "cell", "generator_report": r1}],
            "placement": {
                "strategy": "array",
                "order": ["cell"],
                "rows": 2,
                "cols": 3,
                "row_pitch_um": 5.0,
                "col_pitch_um": 15.0,
                "origin_um": {"x": 1.0, "y": 2.0},
            },
            "options": {"cell_name": "array_top_0", "output": str(output)},
        }
    )
    assert output.is_file()

    entry = report["blocks"][0]
    assert entry["id"] == "cell"
    assert entry["offset_um"] == {"x": 1.0, "y": 2.0}
    expected_bbox = array_placement_bbox_um(
        r1["bbox_um"],
        {
            "rows": 2,
            "cols": 3,
            "row_pitch_um": 5.0,
            "col_pitch_um": 15.0,
            "origin_um": {"x": 1.0, "y": 2.0},
        },
    )
    assert entry["bbox_um"] == pytest.approx(expected_bbox)
    assert report["bbox_um"] == pytest.approx(expected_bbox)

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("array_top_0")

    instances = list(top.each_inst())
    assert len(instances) == 1  # exactly one hierarchical array instance
    inst = instances[0]
    assert inst.is_regular_array()
    # KLayout's GDS AREF read/write path can swap which of its internal
    # a/b vectors (and na/nb counts) maps to which -- so assert on the tile
    # *count* (rows * cols) rather than a specific na<->cols/nb<->rows
    # binding; the bbox check below independently confirms the array grew
    # along the correct axes (col_pitch_um/row_pitch_um are deliberately
    # different values in this request).
    assert inst.cell_inst.na * inst.cell_inst.nb == 2 * 3
    assert {inst.cell_inst.na, inst.cell_inst.nb} == {2, 3}

    # The underlying sub-cell only carries r1's own shapes once -- KLayout's
    # bbox for a regular array instance already spans all 6 tiles.
    inst_bbox_um = {
        "x0": inst.bbox().left * layout.dbu,
        "y0": inst.bbox().bottom * layout.dbu,
        "x1": inst.bbox().right * layout.dbu,
        "y1": inst.bbox().top * layout.dbu,
    }
    assert inst_bbox_um == pytest.approx(expected_bbox)


def test_compose_array_1x1_degenerates_to_single_tile(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "array_1x1.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "cell", "generator_report": r1}],
            "placement": {
                "strategy": "array",
                "order": ["cell"],
                "rows": 1,
                "cols": 1,
                "row_pitch_um": 5.0,
                "col_pitch_um": 5.0,
            },
            "options": {"cell_name": "array_1x1_0", "output": str(output)},
        }
    )
    entry = report["blocks"][0]
    assert entry["offset_um"] == {"x": 0.0, "y": 0.0}
    assert entry["bbox_um"] == pytest.approx(r1["bbox_um"])

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("array_1x1_0")
    instances = list(top.each_inst())
    # A 1x1 "array" is a single tile either way -- KLayout collapses a
    # na=1/nb=1 CellInstArray into a plain (non-array) instance, which is
    # still exactly one instance, satisfying the "one instance, not
    # rows*cols" requirement trivially (rows*cols == 1 here).
    assert len(instances) == 1


def test_compose_array_default_origin_is_zero_zero(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "array_default_origin.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "cell", "generator_report": r1}],
            "placement": {
                "strategy": "array",
                "order": ["cell"],
                "rows": 3,
                "cols": 2,
                "row_pitch_um": 5.0,
                "col_pitch_um": 20.0,
            },
            "options": {"cell_name": "array_default_origin_0", "output": str(output)},
        }
    )
    assert report["blocks"][0]["offset_um"] == {"x": 0.0, "y": 0.0}


def test_compose_array_min_spacing_um_is_null(tmp_path, pdk_root):
    # Neither "explicit" nor "array" reports a single shared min_spacing_um
    # -- "array" has two independent pitches (row/col), not one.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "array_min_spacing.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "cell", "generator_report": r1}],
            "placement": {
                "strategy": "array",
                "order": ["cell"],
                "rows": 2,
                "cols": 2,
                "row_pitch_um": 5.0,
                "col_pitch_um": 20.0,
            },
            "options": {"cell_name": "array_min_spacing_0", "output": str(output)},
        }
    )
    assert report["drc_hints"]["min_spacing_um"] is None


def test_compose_rejects_connectivity_unknown_block(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="unknown block id"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "r", "port": "P1"},
                            {"block": "nonexistent", "port": "P1"},
                        ],
                    }
                ],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_rejects_connectivity_unknown_port(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="unknown port"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "r", "port": "NOPE"},
                            {"block": "r", "port": "P2"},
                        ],
                    }
                ],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_rejects_unresolvable_pdk(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError):
        compose(
            {
                "pdk": {"variant": "nonexistentPDK", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            }
        )


# --------------------------------------------------------------------------- #
# compose() -- end-to-end row placement against real `klt gen` outputs
# --------------------------------------------------------------------------- #


def test_compose_row_places_two_real_blocks(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1", num=2)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2", num=3)

    output = tmp_path / "composed.gds"
    report = compose(
        {
            "schema": gen_compose.REQUEST_SCHEMA,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
            "options": {"cell_name": "composed_0", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["schema_version"] == 1
    assert report["cell_name"] == "composed_0"
    assert report["gds_path"] == str(output)
    assert report["pdk"] == {"name": "sky130A", "variant": "sky130A", "version": None}
    assert report["nets"] == []
    assert report["unrouted_nets"] == []
    assert report["drc_hints"] == {
        "min_spacing_um": None,
        "matched_groups": [],
        "notes": [],
    }
    assert report["warnings"] == []

    blocks = {b["id"]: b for b in report["blocks"]}
    assert blocks["b1"]["generator"] == "resistor_strip"
    assert blocks["b1"]["offset_um"] == {"x": 0.0, "y": 0.0}
    assert blocks["b1"]["bbox_um"] == pytest.approx(r1["bbox_um"])

    b1_width = r1["bbox_um"]["x1"] - r1["bbox_um"]["x0"]
    assert blocks["b2"]["offset_um"]["x"] == pytest.approx(b1_width + 1.0)
    assert blocks["b2"]["offset_um"]["y"] == pytest.approx(0.0)

    # Composed bbox is the union of both translated blocks.
    assert report["bbox_um"]["x0"] == pytest.approx(
        min(0.0, blocks["b2"]["bbox_um"]["x0"])
    )
    assert report["bbox_um"]["x1"] == pytest.approx(blocks["b2"]["bbox_um"]["x1"])

    # Verify the actual GDS: two child cell instances at the reported offsets.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("composed_0")
    assert top is not None
    insts = list(top.each_inst())
    assert len(insts) == 2
    dbu = layout.dbu
    offsets_seen = sorted(
        (round(inst.trans.disp.x * dbu, 6), round(inst.trans.disp.y * dbu, 6))
        for inst in insts
    )
    expected = sorted(
        (
            round(blocks[bid]["offset_um"]["x"], 6),
            round(blocks[bid]["offset_um"]["y"], 6),
        )
        for bid in ("b1", "b2")
    )
    assert offsets_seen == expected


def test_compose_output_is_byte_reproducible(tmp_path, pdk_root):
    """Two `compose()` runs with identical blocks/placement/inputs must
    produce byte-identical GDS streams (#320), matching `klt gen`'s
    reproducibility guarantee -- see
    `test_gen.test_generate_output_is_byte_reproducible`."""
    import time

    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1", num=2)
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2", num=3)

    def _compose_request(output):
        return {
            "schema": gen_compose.REQUEST_SCHEMA,
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
            "options": {"cell_name": "composed_0", "output": str(output)},
        }

    output_a = tmp_path / "composed_a.gds"
    compose(_compose_request(output_a))
    time.sleep(1.1)
    output_b = tmp_path / "composed_b.gds"
    compose(_compose_request(output_b))

    assert output_a.read_bytes() == output_b.read_bytes()


def test_compose_single_block_degenerate_case(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "solo")
    output = tmp_path / "solo_composed.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "solo", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["solo"], "spacing_um": 2.0},
            "options": {"cell_name": "solo_composed", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["blocks"][0]["offset_um"] == {"x": 0.0, "y": 0.0}
    assert report["bbox_um"] == pytest.approx(r1["bbox_um"])


def test_compose_accepts_unmodified_draw_report_as_a_block(tmp_path, pdk_root):
    """A `klt draw` response (issue #1059), fed straight into
    `blocks[].generator_report` with no hand-patching, must compose --
    `draw`'s output already carries `generator: "draw"` plus the
    `cell_name`/`gds_path`/`bbox_um` fields `_parse_blocks` requires, and has
    no `ports[]` key at all (which `_parse_blocks` already defaults to
    `[]`)."""
    from klayout_tools.draw import REQUEST_SCHEMA as DRAW_REQUEST_SCHEMA
    from klayout_tools.draw import draw

    draw_output = tmp_path / "solo_drawn.gds"
    draw_report = draw(
        {
            "schema": DRAW_REQUEST_SCHEMA,
            "params": {
                "shapes": [
                    {
                        "layer": [66, 20],
                        "name": "poly.drawing",
                        "rect_um": [0, 0, 1.0, 2.0],
                    }
                ]
            },
            "options": {"cell_name": "solo_drawn", "output": str(draw_output)},
        }
    )
    assert draw_report["generator"] == "draw"
    assert "ports" not in draw_report

    output = tmp_path / "solo_drawn_composed.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "solo", "generator_report": draw_report}],
            "placement": {"strategy": "row", "order": ["solo"], "spacing_um": 2.0},
            "options": {"cell_name": "solo_drawn_composed", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["blocks"][0]["generator"] == "draw"
    assert report["bbox_um"] == pytest.approx(draw_report["bbox_um"])


def test_compose_accepts_inline_and_file_generator_report(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    r2_path = tmp_path / "r2.json"
    r2_path.write_text(json.dumps(r2))

    output = tmp_path / "mixed.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "inline", "generator_report": r1},
                {"id": "from_file", "generator_report": str(r2_path)},
            ],
            "placement": {
                "strategy": "row",
                "order": ["inline", "from_file"],
                "spacing_um": 0.5,
            },
            "options": {"cell_name": "mixed_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert {b["id"] for b in report["blocks"]} == {"inline", "from_file"}


# --------------------------------------------------------------------------- #
# manhattan_backbone() -- pure routing geometry, no PDK/pya involvement
# --------------------------------------------------------------------------- #


def test_manhattan_backbone_straight_when_aligned_facing_ports():
    # Port a at (0,1) facing +x, port b at (10,1) facing -x -- same y, so the
    # backbone collapses to a straight segment (stubs + degenerate jog removed).
    points = manhattan_backbone((0.0, 1.0), 0, (10.0, 1.0), 180, stub_um=0.5)
    assert points == [(0.0, 1.0), (10.0, 1.0)]


def test_manhattan_backbone_z_jog_when_horizontal_ports_offset_in_y():
    # a at (0,0) facing +x, b at (10,4) facing -x -- a single vertical jog at
    # the midpoint x between the two stub ends joins the two horizontal runs.
    points = manhattan_backbone((0.0, 0.0), 0, (10.0, 4.0), 180, stub_um=1.0)
    # First and last points are the ports themselves.
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (10.0, 4.0)
    # Exactly one vertical jog: two interior corners at a shared x (the midpoint
    # of the stub ends: (1.0 + 9.0)/2 = 5.0).
    xs = [p[0] for p in points]
    assert xs.count(5.0) == 2
    corners = [p for p in points if p[0] == 5.0]
    assert corners == [(5.0, 0.0), (5.0, 4.0)]
    # Every segment is orthogonal (shares an x or a y with its neighbour).
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        assert x0 == x1 or y0 == y1


def test_manhattan_backbone_l_corner_for_mixed_orientation():
    # a faces +x, b faces +y -- the two stubs join at a single corner (an "L").
    points = manhattan_backbone((0.0, 0.0), 0, (6.0, 6.0), 90, stub_um=1.0)
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (6.0, 6.0)
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        assert x0 == x1 or y0 == y1


def test_manhattan_backbone_waypoints_route_through_supplied_points_in_order():
    # #634: two west-facing ports (same absolute direction) in a row -- the
    # fixed one-jog shape (see test_manhattan_backbone_straight_when_aligned_
    # facing_ports above) would collapse to a straight line straight through
    # block a's own interior. Caller-supplied waypoints route up and over
    # instead: both waypoints already share an x with their neighbouring stub,
    # so no extra elbow is needed here (see the single-waypoint test below for
    # that case).
    points = manhattan_backbone(
        (0.0, 5.0),
        180,
        (12.0, 5.0),
        180,
        stub_um=0.3,
        waypoints=[(-0.3, 11.0), (11.7, 11.0)],
    )
    assert points[0] == (0.0, 5.0)
    assert points[-1] == (12.0, 5.0)
    assert (-0.3, 11.0) in points
    assert (11.7, 11.0) in points
    # Every segment is orthogonal (shares an x or a y with its neighbour).
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        assert x0 == x1 or y0 == y1


def test_manhattan_backbone_waypoints_inserts_elbow_for_a_non_aligned_point():
    # A single waypoint that shares neither the source stub's x/y nor the
    # destination stub's x/y forces an elbow corner on *each* side (move in x
    # first, then y -- manhattan_backbone's own documented convention), so the
    # whole path stays a strictly axis-aligned polyline even though the
    # caller supplied only one point.
    points = manhattan_backbone(
        (0.0, 0.21),
        180,
        (11.26, 0.21),
        180,
        stub_um=0.17,
        waypoints=[(5.545, 1.0)],
    )
    assert points[0] == (0.0, 0.21)
    assert points[-1] == (11.26, 0.21)
    assert (5.545, 1.0) in points
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        assert x0 == x1 or y0 == y1


def test_manhattan_backbone_omitting_waypoints_keeps_the_fixed_jog_shape():
    # Regression: waypoints=None (the default) must reproduce the exact
    # pre-#634 output -- same test data as
    # test_manhattan_backbone_z_jog_when_horizontal_ports_offset_in_y above.
    points = manhattan_backbone((0.0, 0.0), 0, (10.0, 4.0), 180, stub_um=1.0)
    assert points == manhattan_backbone(
        (0.0, 0.0), 0, (10.0, 4.0), 180, stub_um=1.0, waypoints=None
    )


def test_cleanup_points_removes_duplicates_and_collinear():
    raw = [(0.0, 0.0), (0.0, 0.0), (2.0, 0.0), (5.0, 0.0), (5.0, 3.0)]
    # Two collapse: the duplicate origin and the collinear midpoint (2,0).
    assert _cleanup_points(raw) == [(0.0, 0.0), (5.0, 0.0), (5.0, 3.0)]


# --------------------------------------------------------------------------- #
# _resolve_label_layer() / _polyline_midpoint_um() -- #200 net-labelling
# --------------------------------------------------------------------------- #


def test_resolve_label_layer_sky130_metal_role_is_li1_pin():
    # "metal" resolves to li1.drawing (67/20), metals[0] in sky130's
    # ExtractionDeck -- its paired label is li1.pin (67/5), metal_labels[0].
    assert _resolve_label_layer("sky130A", (67, 20)) == (67, 5)


def test_resolve_label_layer_gf180mcu_metal_role_is_metal1_pin():
    # "metal" resolves to Metal1 (34/0), gf180mcu's sole metals[] entry --
    # its paired label is Metal1's pin/label purpose (34/10).
    assert _resolve_label_layer("gf180mcuA", (34, 0)) == (34, 10)


def test_resolve_label_layer_sky130_poly_resolves_to_poly_pin():
    # #210: "poly" (66/20 on sky130) is not a `metals[]` entry, but the
    # ExtractionDeck now pairs it with a poly-label layer (poly.pin, 66/5) so a
    # bare-poly gate node can be named without a metal landing pad.
    assert _resolve_label_layer("sky130A", (66, 20)) == (66, 5)


def test_resolve_label_layer_gf180mcu_poly_resolves_to_poly_label():
    # #210: Poly2 (30/0 on gf180mcu) pairs with its datatype-10 label purpose.
    assert _resolve_label_layer("gf180mcuA", (30, 0)) == (30, 10)


def test_resolve_label_layer_returns_none_for_a_layer_with_no_label_convention():
    # A drawn layer that is neither a `metals[]` entry nor the deck's `poly`
    # layer has no label-layer convention -- a shape on it draws without a net
    # label rather than raising or guessing a layer. `contact` (licon1, 66/44
    # on sky130) is such a layer.
    assert _resolve_label_layer("sky130A", (66, 44)) is None


def test_polyline_midpoint_um_straight_line_is_geometric_midpoint():
    assert _polyline_midpoint_um([(0.0, 0.0), (2.0, 0.0)]) == (1.0, 0.0)


def test_polyline_midpoint_um_jogged_route_is_arc_length_midpoint():
    # Total arc length is 1 + 4 = 5um; the midpoint (2.5um in) falls 1.5um
    # into the second (vertical) segment.
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 4.0)]
    assert _polyline_midpoint_um(points) == (1.0, 1.5)


def test_polyline_midpoint_um_degenerate_zero_length_falls_back_to_first_point():
    assert _polyline_midpoint_um([(3.0, 4.0), (3.0, 4.0)]) == (3.0, 4.0)


# --------------------------------------------------------------------------- #
# compose() -- routing (phase 2)
# --------------------------------------------------------------------------- #


def test_compose_routes_two_pin_net_between_adjacent_blocks(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "wired.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "wired_0", "output": str(output)},
        }
    )
    # phase 2: the net is routed end-to-end; nothing left unrouted.
    assert len(report["nets"]) == 1
    net = report["nets"][0]
    assert net["net"] == "N1"
    assert net["routed"] is True
    # Both resistor_strip ports sit at the same y (width/2), so the route is a
    # straight span of exactly the placement gap (spacing_um = 1.0).
    assert net["route_length_um"] == pytest.approx(1.0)
    assert report["unrouted_nets"] == []
    assert report["drc_hints"]["min_spacing_um"] == pytest.approx(1.0)

    # The composed GDS carries a metal (li1 = 67/20) path on the top cell.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("wired_0")
    li1 = layout.layer(67, 20)
    paths = [s for s in top.shapes(li1).each() if s.is_path()]
    assert len(paths) == 1
    width_dbu = int(round(0.17 / layout.dbu))
    assert paths[0].path.width == width_dbu

    # #200: the routed net also carries a kdb.Text label on li1.pin (67/5),
    # named after connectivity[].net -- exactly one, not one per segment.
    li1_pin = layout.layer(67, 5)
    texts = list(top.shapes(li1_pin).each())
    assert len(texts) == 1
    assert texts[0].text.string == "N1"

    # The label sits strictly in the inter-block channel -- not inside
    # either block's own bbox (see route_two_pin's obstacle-overlap check
    # and _polyline_midpoint_um's docstring for why this is guaranteed).
    b1_bbox = report["blocks"][0]["bbox_um"]
    b2_bbox = report["blocks"][1]["bbox_um"]
    label_x_um = texts[0].text.x * layout.dbu
    assert b1_bbox["x1"] < label_x_um < b2_bbox["x0"]


def test_compose_labels_jogged_route_exactly_once_not_per_segment(tmp_path, pdk_root):
    # A mixed-orientation port pair (see test_manhattan_backbone_l_corner_for
    # _mixed_orientation) produces a multi-segment backbone drawn as one
    # kdb.Path -- confirm the label count stays at one per net regardless of
    # how many straight segments make up the drawn path.
    #
    # `gate_contact=True` (#492) puts the gate terminal on the metal role, so
    # the metal backbone actually lands on a contacted pad -- without it the
    # gate is bare poly and this net is now rejected outright rather than
    # drawn as an uncontacted stub (see
    # test_compose_rejects_metal_route_to_bare_poly_gate_port).
    m1 = _gen_block(
        tmp_path, pdk_root, "mos_array", "m1", rows=1, cols=1, gate_contact=True
    )
    m2 = _gen_block(
        tmp_path, pdk_root, "mos_array", "m2", rows=1, cols=1, gate_contact=True
    )
    output = tmp_path / "jogged.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": m1},
                {"id": "b2", "generator_report": m2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 2.0},
            "connectivity": [
                {
                    "net": "GNET",
                    "pins": [
                        {"block": "b1", "port": "U0_G"},
                        {"block": "b2", "port": "U0_G"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "jogged_0", "output": str(output)},
        }
    )
    assert report["nets"][0]["routed"] is True

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("jogged_0")
    li1_pin = layout.layer(67, 5)
    texts = list(top.shapes(li1_pin).each())
    assert len(texts) == 1
    assert texts[0].text.string == "GNET"


def test_compose_notes_missing_label_convention_for_unlabelled_route_layer(
    tmp_path, pdk_root
):
    # "tap" is a valid routable role but is neither a `metals[]` entry nor the
    # deck's `poly` layer, so `_resolve_label_layer` finds no label convention
    # for it -- the metal is still drawn, but no label, and a note explains
    # why. (Since #210 gave `poly` a `poly_label`, poly no longer exercises
    # this path; `tap` still does.)
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "unlabelled.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "tap", "width_um": 0.17},
            "options": {"cell_name": "unlabelled_0", "output": str(output)},
        }
    )
    assert report["nets"][0]["routed"] is True
    assert any(
        "no PDK label-layer convention" in note for note in report["drc_hints"]["notes"]
    )

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("unlabelled_0")
    tap = layout.layer(65, 44)
    paths = [s for s in top.shapes(tap).each() if s.is_path()]
    assert len(paths) == 1  # metal still drawn
    li1_pin = layout.layer(67, 5)
    assert list(top.shapes(li1_pin).each()) == []  # but no label anywhere


def test_compose_reports_unroutable_net_as_partial_success(tmp_path, pdk_root):
    # Two 2-row blocks placed hard against each other (spacing 0). The wired
    # ports both face along x (toward each other) but sit on *different rows*
    # (different y), so a vertical jog is required -- and the channel between
    # the touching blocks (0um) is narrower than the wide requested route
    # width, so the net cannot be routed. The blocks still place: partial
    # success.
    r1 = _gen_block(tmp_path, pdk_root, "mos_array", "m1", rows=2, cols=1)
    r2 = _gen_block(tmp_path, pdk_root, "mos_array", "m2", rows=2, cols=1)
    output = tmp_path / "unroutable.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 0.0},
            "connectivity": [
                {
                    # U0_D faces +x (row 0), U1_S faces -x (row 1) -- both
                    # horizontal but at different y, forcing a vertical jog
                    # through the zero-width gap between the touching blocks.
                    "net": "BADNET",
                    "pins": [
                        {"block": "b1", "port": "U0_D"},
                        {"block": "b2", "port": "U1_S"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 5.0},
            "options": {"cell_name": "unroutable_0", "output": str(output)},
        }
    )
    assert output.is_file()  # blocks still placed
    assert report["unrouted_nets"] == ["BADNET"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["status"] == "unrouted"  # 0 of 1 legs drawn, #1169
    assert report["nets"][0]["route_length_um"] is None
    assert any("BADNET" in note for note in report["drc_hints"]["notes"])
    # #1198: routing was requested (not declare-only) but every net's every
    # leg failed to route -- zero metal drawn -- so `min_spacing_um` must
    # still be null, not the row's own (here 0.0) spacing_um.
    assert report["drc_hints"]["min_spacing_um"] is None


# --------------------------------------------------------------------------- #
# Bundle (>2-pin) nets (#1073): a shared supply rail or fanout node touches one
# port on every block it spans, so a two-pin-only router cannot wire the
# majority of a real circuit's connectivity. `route_bundle` routes such a net
# as a spanning tree of two-pin legs (nearest pair first), every leg going
# through `route_two_pin` unchanged so all of its routability checks apply per
# leg.
# --------------------------------------------------------------------------- #


def _gate_rail_blocks(tmp_path, pdk_root, count, cols=1, prefix="rail_m"):
    """`count` 1-row mos_arrays whose gates are on the metal role.

    `gate_contact=True` (#492) puts the gate terminal on `metal`, so a metal
    rail actually lands on a contacted pad; `dummy=0` keeps a dummy column's
    own (also contacted) gate pad out of the inter-gate channel the rail runs
    through -- the same fixture shape
    `test_compose_routes_gate_contact_port_end_to_end` already uses for the
    two-pin case.
    """
    return [
        _gen_block(
            tmp_path,
            pdk_root,
            "mos_array",
            f"{prefix}{index}",
            rows=1,
            cols=cols,
            dummy=0,
            gate_contact=True,
        )
        for index in range(count)
    ]


def _gate_rail_request(pdk_root, reports, pin_order, output, cell_name, net="VBIAS"):
    ids = [f"b{index + 1}" for index in range(len(reports))]
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": block_id, "generator_report": report}
            for block_id, report in zip(ids, reports, strict=True)
        ],
        "placement": {"strategy": "row", "order": ids, "spacing_um": 2.0},
        "connectivity": [
            {"net": net, "pins": [dict(pin) for pin in pin_order]},
        ],
        # A pad-wide trace: both gate ports face north, so a trace narrower
        # than the pad would leave a sub-li1.space.1 slit beside each stub
        # (see test_compose_routes_gate_contact_port_end_to_end's own note).
        "routing": {"layer_role": "metal", "width_um": 0.42},
        "options": {"cell_name": cell_name, "output": str(output)},
    }


def test_compose_routes_three_pin_rail_shared_by_three_blocks(tmp_path, pdk_root):
    # The issue's own reproduction: three blocks sharing one supply/bias rail.
    # Before #1073 this came back in `unrouted_nets[]` (exit 3) with nothing
    # drawn; it must now be routed as a real, single conductor.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 3)
    output = tmp_path / "rail3.gds"
    report = compose(
        _gate_rail_request(
            pdk_root,
            reports,
            [{"block": f"b{i}", "port": "U0_G"} for i in (1, 2, 3)],
            output,
            "rail3_0",
        )
    )

    assert report["unrouted_nets"] == []
    net = report["nets"][0]
    assert net["routed"] is True
    assert net["status"] == "routed"  # full spanning tree, #1169
    # A 3-pin net spans with 2 legs, each between adjacent blocks in the row.
    assert [sorted(pin["block"] for pin in leg["pins"]) for leg in net["legs"]] == [
        ["b1", "b2"],
        ["b2", "b3"],
    ]
    assert all(leg["routed"] is True for leg in net["legs"])
    assert net["route_length_um"] == pytest.approx(
        sum(leg["route_length_um"] for leg in net["legs"])
    )

    # All three gate pads are one merged li1 polygon -- a real rail, not three
    # stubs (the whole point of the issue).
    positions = []
    for index, block_report in enumerate(reports):
        gate = next(p for p in block_report["ports"] if p["name"] == "U0_G")
        offset = report["blocks"][index]["offset_um"]
        positions.append((gate["x_um"] + offset["x"], gate["y_um"] + offset["y"]))
    assert _shares_merged_polygon(output, "rail3_0", 67, 20, positions[0], positions[1])
    assert _shares_merged_polygon(output, "rail3_0", 67, 20, positions[0], positions[2])

    # One kdb.Text for the net, not one per leg -- the legs are one conductor.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    texts = list(layout.cell("rail3_0").shapes(layout.layer(67, 5)).each())
    assert [text.text.string for text in texts] == ["VBIAS"]

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    # ...and the loop closes: extraction sees all three gates on one named net.
    result = extract.run_extract(str(output), "sky130", top="rail3_0")
    gate_nets = [d["nets"]["g"] for d in result["devices"] if d["class"] == "nfet"]
    assert gate_nets == ["VBIAS", "VBIAS", "VBIAS"], result["devices"]
    assert "VBIAS" in {net["name"] for net in result["nets"] if net["pin"]}


def test_compose_routes_four_pin_rail_regardless_of_pin_declaration_order(
    tmp_path, pdk_root
):
    # 4 pins spanning 4 blocks, declared in a scrambled order: the spanning
    # tree is built nearest-pair-first, so the caller's `pins[]` order is not
    # a routing order -- a chain declared out of order still routes.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 4)
    output = tmp_path / "rail4.gds"
    report = compose(
        _gate_rail_request(
            pdk_root,
            reports,
            [{"block": f"b{i}", "port": "U0_G"} for i in (3, 1, 4, 2)],
            output,
            "rail4_0",
        )
    )

    assert report["unrouted_nets"] == []
    net = report["nets"][0]
    assert net["routed"] is True
    # 4 pins -> exactly 3 legs, every one of them between *adjacent* blocks
    # (the nearest-first ordering is what makes this a trunk, not a star).
    assert len(net["legs"]) == 3
    for leg in net["legs"]:
        left, right = sorted(int(pin["block"][1:]) for pin in leg["pins"])
        assert right - left == 1, leg

    result = extract.run_extract(str(output), "sky130", top="rail4_0")
    gate_nets = [d["nets"]["g"] for d in result["devices"] if d["class"] == "nfet"]
    assert gate_nets == ["VBIAS"] * 4, result["devices"]
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_compose_routes_bundle_net_mixing_a_self_net_leg_and_a_cross_block_leg(
    tmp_path, pdk_root
):
    # A fanout node that taps two terminals of one block plus one of another:
    # the nearest leg is a *self*-net (both pins on b1), which goes through
    # exactly the same route_two_pin self-net checks (#433/#453/#469) a 2-pin
    # self-net does.
    b1 = _gate_rail_blocks(tmp_path, pdk_root, 1, cols=2, prefix="hub_a")[0]
    b2 = _gate_rail_blocks(tmp_path, pdk_root, 1, prefix="hub_b")[0]
    output = tmp_path / "hub.gds"
    report = compose(
        _gate_rail_request(
            pdk_root,
            [b1, b2],
            [
                {"block": "b1", "port": "U0_G"},
                {"block": "b1", "port": "U1_G"},
                {"block": "b2", "port": "U0_G"},
            ],
            output,
            "hub_0",
        )
    )

    assert report["unrouted_nets"] == []
    net = report["nets"][0]
    assert net["routed"] is True
    assert len(net["legs"]) == 2
    # The self-net leg (both pins on b1) is the nearest pair, so it is tried
    # -- and accepted -- first.
    assert [pin["block"] for pin in net["legs"][0]["pins"]] == ["b1", "b1"]

    result = extract.run_extract(str(output), "sky130", top="hub_0")
    gate_nets = [d["nets"]["g"] for d in result["devices"] if d["class"] == "nfet"]
    assert gate_nets == ["VBIAS", "VBIAS", "VBIAS"], result["devices"]


def test_compose_reports_per_leg_reasons_when_a_bundle_pin_cannot_be_reached(
    tmp_path, pdk_root
):
    # The pre-#1073 fixture (this test replaces
    # `test_compose_defers_bundle_net_as_unrouted`, which asserted the net was
    # deferred *because* it had >2 pins). The net is still not fully
    # connected -- but now for a real, per-leg geometric reason, reported per
    # leg rather than as a blanket "bundle routing is out of scope". Since
    # #1169, the one leg that *is* individually routable (b1.P2-b2.P1) is now
    # drawn rather than discarded -- this is the "2-leg net (3 pins), one leg
    # unroutable" partial-routing case.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "bundle.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "BUS",
                    "pins": [
                        {"block": "b1", "port": "P1"},
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "bundle_0", "output": str(output)},
        }
    )

    # Still not fully routed -- unrouted_nets[] still names it -- but it is a
    # *partial* success now, not a zero-geometry one.
    assert report["unrouted_nets"] == ["BUS"]
    net = report["nets"][0]
    assert net["routed"] is False
    assert net["status"] == "partial"
    assert net["route_length_um"] is not None  # the one drawn leg's length
    # #1198: one leg of BUS actually drew metal through the row's placement
    # gap, so min_spacing_um reports it -- "partial" is still "something was
    # routed", unlike the declare-only/all-unroutable cases above.
    assert report["drc_hints"]["min_spacing_um"] == pytest.approx(1.0)

    legs = {tuple(_pin_ref(pin) for pin in leg["pins"]): leg for leg in net["legs"]}
    # The b1.P1 leg across b1's own body is rejected by the self-net
    # drawn-metal check (#453/#469) -- the per-leg reason names it...
    self_leg = legs[("b1.P1", "b1.P2")]
    assert self_leg["routed"] is False
    assert "drawn" in self_leg["reason"]
    # ...and the router still tried the farther alternative to b1.P1, which
    # the obstacle check rejects for plowing through b1.
    around_leg = legs[("b1.P1", "b2.P1")]
    assert around_leg["routed"] is False
    assert "crosses" in around_leg["reason"]
    # The b1.P2-b2.P1 leg is routable on its own, and #1169 now draws it even
    # though b1.P1 is stranded: `legs[].routed` still means "drawn", but a
    # leg no longer needs its whole net to succeed to earn that.
    connectable_leg = legs[("b1.P2", "b2.P1")]
    assert connectable_leg["routed"] is True
    assert connectable_leg["reason"] is None
    assert connectable_leg["route_length_um"] == pytest.approx(net["route_length_um"])

    note = next(note for note in report["drc_hints"]["notes"] if "BUS" in note)
    assert "'b1.P1'" in note  # names the pin that could not be reached
    assert "nets[].legs[]" in note

    # Partial routing: the one routable leg's metal is drawn, the rejected
    # legs' metal is not.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("bundle_0")
    paths = [s for s in top.shapes(layout.layer(67, 20)).each() if s.is_path()]
    assert len(paths) == 1  # exactly the connectable leg's backbone, not 0

    # The drawn subset must itself stay DRC-clean (#1169 acceptance criteria).
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_compose_bundle_net_tries_every_candidate_leg_to_an_unreachable_pin(
    tmp_path, pdk_root
):
    # A pin behind a closed guard ring cannot be reached from anywhere, so
    # *both* candidate legs into it are attempted and rejected -- evidence
    # that a rejected leg is retried against another partner rather than
    # failing the net at the first rejection. Since #1169, the b1-b2 leg --
    # individually routable and unrelated to the ring obstacle -- is drawn
    # even though b3 ends up stranded: the "2-leg net (3 pins), one leg
    # unroutable" partial-routing case, this time via a real closed-ring
    # obstacle rather than a self-net one.
    m1, m2 = _gate_rail_blocks(tmp_path, pdk_root, 2)
    ringed = _gen_block(tmp_path, pdk_root, "diff_pair", "ringed", splits=1)
    output = tmp_path / "ringed_bundle.gds"
    report = compose(
        _gate_rail_request(
            pdk_root,
            [m1, m2, ringed],
            [
                {"block": "b1", "port": "U0_G"},
                {"block": "b2", "port": "U0_G"},
                {"block": "b3", "port": "Q1_1_S"},
            ],
            output,
            "ringed_bundle_0",
        )
    )

    assert report["unrouted_nets"] == ["VBIAS"]
    net = report["nets"][0]
    assert net["routed"] is False
    assert net["status"] == "partial"
    assert net["route_length_um"] is not None  # the b1-b2 leg's length
    ring_legs = [
        leg for leg in net["legs"] if any(pin["block"] == "b3" for pin in leg["pins"])
    ]
    assert len(ring_legs) == 2  # both partners tried
    assert all("closed guard/collector ring" in leg["reason"] for leg in ring_legs)
    assert all(leg["routed"] is False for leg in ring_legs)

    # The b1-b2 leg has nothing to do with the ring obstacle -- it is drawn.
    bridge_leg = next(
        leg
        for leg in net["legs"]
        if {pin["block"] for pin in leg["pins"]} == {"b1", "b2"}
    )
    assert bridge_leg["routed"] is True
    assert bridge_leg["reason"] is None
    assert bridge_leg["route_length_um"] == pytest.approx(net["route_length_um"])

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("ringed_bundle_0")
    paths = [s for s in top.shapes(layout.layer(67, 20)).each() if s.is_path()]
    assert len(paths) == 1  # exactly the b1-b2 backbone

    # The drawn subset must itself stay DRC-clean (#1169 acceptance criteria).
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_route_bundle_falls_back_to_a_farther_leg_when_the_nearest_is_rejected(
    tmp_path, pdk_root
):
    # Direct `route_bundle` test of the `leg_conflict` hook -- the same hook
    # `compose()` passes its route-vs-route collision check (#1057) through.
    # Rejecting the nearest leg (b1-b2) must not fail the net: the next
    # candidate that joins the same two parts (b1-b3, spanning over b2) is
    # tried instead.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 3)
    ids = ["b1", "b2", "b3"]
    blocks = gen_compose._parse_blocks(
        [
            {"id": block_id, "generator_report": report}
            for block_id, report in zip(ids, reports, strict=True)
        ]
    )
    bboxes = {block_id: blocks[block_id]["bbox_um"] for block_id in ids}
    offsets = compute_row_offsets(ids, bboxes, spacing_um=2.0)
    placed = {
        block_id: _translate_bbox(bboxes[block_id], offsets[block_id])
        for block_id in ids
    }
    pins = [{"block": block_id, "port": "U0_G"} for block_id in ids]
    route_layer = gen_compose._resolve_route_layer("sky130A", "metal")

    def _gate_x(block_id):
        gate = blocks[block_id]["ports"]["U0_G"]
        return round(gate["x_um"] + offsets[block_id]["x"], 6)

    def _reject_b1_b2(points_um, via_drops=None, stub_widen=None):
        endpoints = {round(points_um[0][0], 6), round(points_um[-1][0], 6)}
        if endpoints == {_gate_x("b1"), _gate_x("b2")}:
            return "simulated collision with an already-routed net"
        return None

    baseline = gen_compose.route_bundle(
        pins, blocks, offsets, placed, 0.42, route_layer
    )
    assert baseline["routed"] is True
    assert [
        sorted(pin["block"] for pin in leg["pins"]) for leg in baseline["legs"]
    ] == [["b1", "b2"], ["b2", "b3"]]

    result = gen_compose.route_bundle(
        pins,
        blocks,
        offsets,
        placed,
        0.42,
        route_layer,
        leg_conflict=_reject_b1_b2,
    )
    assert result["routed"] is True
    accepted = [
        sorted(pin["block"] for pin in leg["pins"])
        for leg in result["legs"]
        if leg["routed"]
    ]
    assert ["b1", "b2"] not in accepted
    # Still a spanning tree: 2 legs covering all three blocks.
    assert len(accepted) == 2
    assert {block_id for leg in accepted for block_id in leg} == set(ids)


def _route_bundle_row_fixture(tmp_path, pdk_root, count, spacing_um=2.0):
    """Shared setup for a direct `route_bundle()` call over `count` blocks in
    a row -- factored out of
    `test_route_bundle_falls_back_to_a_farther_leg_when_the_nearest_is_rejected`
    so #1169's partial-routing tests below can reuse it.
    """
    reports = _gate_rail_blocks(tmp_path, pdk_root, count)
    ids = [f"b{index + 1}" for index in range(count)]
    blocks = gen_compose._parse_blocks(
        [
            {"id": block_id, "generator_report": report}
            for block_id, report in zip(ids, reports, strict=True)
        ]
    )
    bboxes = {block_id: blocks[block_id]["bbox_um"] for block_id in ids}
    offsets = compute_row_offsets(ids, bboxes, spacing_um=spacing_um)
    placed = {
        block_id: _translate_bbox(bboxes[block_id], offsets[block_id])
        for block_id in ids
    }
    pins = [{"block": block_id, "port": "U0_G"} for block_id in ids]
    route_layer = gen_compose._resolve_route_layer("sky130A", "metal")

    def _gate_x(block_id):
        gate = blocks[block_id]["ports"]["U0_G"]
        return round(gate["x_um"] + offsets[block_id]["x"], 6)

    return ids, blocks, offsets, placed, pins, route_layer, _gate_x


def test_route_bundle_draws_the_routable_legs_of_a_4_pin_net_when_the_bridge_fails(
    tmp_path, pdk_root
):
    # 4 pins -> a full spanning tree needs 3 legs. `leg_conflict` rejects
    # every candidate leg that would bridge the {b1, b2} / {b3, b4} halves
    # (the same hook `compose()` wires its real route-vs-route collision
    # check, #1057, through -- an "obstacle" in exactly the mechanism this
    # test suite already uses for it), leaving the two *within-half* legs
    # (b1-b2, b3-b4) as the only two individually routable candidates. Before
    # #1169, a spanning-tree failure discarded *all* of a net's geometry,
    # including these two routable legs; #1169 requires them drawn, and the
    # net reported `status: "partial"` (2 of the 3 legs a full tree needs),
    # not silently dropped.
    ids, blocks, offsets, placed, pins, route_layer, _gate_x = (
        _route_bundle_row_fixture(tmp_path, pdk_root, 4)
    )
    halves = ({"b1", "b2"}, {"b3", "b4"})

    def _block_of(x):
        return next(block_id for block_id in ids if _gate_x(block_id) == x)

    def _reject_cross_half_bridge(points_um, via_drops=None, stub_widen=None):
        endpoints = {round(points_um[0][0], 6), round(points_um[-1][0], 6)}
        blocks_touched = {_block_of(x) for x in endpoints}
        if any(blocks_touched <= half for half in halves):
            return None  # within one half -- not a bridge, always routable
        return "simulated collision with an already-routed net"

    result = gen_compose.route_bundle(
        pins,
        blocks,
        offsets,
        placed,
        0.42,
        route_layer,
        leg_conflict=_reject_cross_half_bridge,
    )

    assert result["routed"] is False
    assert result["status"] == "partial"
    assert result["route_length_um"] is not None

    drawn = [leg for leg in result["legs"] if leg["routed"]]
    assert sorted(sorted(pin["block"] for pin in leg["pins"]) for leg in drawn) == [
        ["b1", "b2"],
        ["b3", "b4"],
    ]
    assert result["route_length_um"] == pytest.approx(
        sum(leg["route_length_um"] for leg in drawn)
    )
    for leg in drawn:
        assert leg["reason"] is None

    rejected = [leg for leg in result["legs"] if not leg["routed"]]
    assert rejected  # every bridging candidate was tried and rejected
    assert all("simulated collision" in leg["reason"] for leg in rejected)
    assert result["reason"] is not None  # net-level reason still reported


def test_route_bundle_reports_unrouted_not_partial_when_zero_legs_drawn(
    tmp_path, pdk_root
):
    # 0-of-N legs routed must stay "unrouted", never "partial" with an empty
    # drawn set -- the acceptance criterion #1169 calls out explicitly (a
    # net with zero drawn legs is not meaningfully different from the
    # pre-#1169 all-or-nothing failure, and must report identically).
    ids, blocks, offsets, placed, pins, route_layer, _gate_x = (
        _route_bundle_row_fixture(tmp_path, pdk_root, 3)
    )

    def _reject_everything(points_um, via_drops=None, stub_widen=None):
        return "simulated collision with an already-routed net"

    result = gen_compose.route_bundle(
        pins,
        blocks,
        offsets,
        placed,
        0.42,
        route_layer,
        leg_conflict=_reject_everything,
    )

    assert result["routed"] is False
    assert result["status"] == "unrouted"
    assert result["route_length_um"] is None
    assert result["legs"]  # every candidate pair was still tried...
    assert all(leg["routed"] is False for leg in result["legs"])
    assert all("simulated collision" in leg["reason"] for leg in result["legs"])


def test_compose_rejects_waypoints_um_on_a_bundle_net(tmp_path, pdk_root):
    # `waypoints_um` (#634) steers *one* backbone; a >2-pin net routes as a
    # spanning tree of legs, so there is no unambiguous leg for it to belong
    # to -- an application error at parse time, never a silently ignored field.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 3)
    request = _gate_rail_request(
        pdk_root,
        reports,
        [{"block": f"b{i}", "port": "U0_G"} for i in (1, 2, 3)],
        tmp_path / "waypoint_bundle.gds",
        "waypoint_bundle_0",
    )
    request["connectivity"][0]["waypoints_um"] = [[1.0, 5.0]]
    with pytest.raises(GenComposeError, match="only supported for a 2-pin net"):
        compose(request)


def test_compose_reports_a_two_pin_net_as_a_single_leg(tmp_path, pdk_root):
    # The 2-pin path is the degenerate one-leg case of the same router --
    # `nets[].legs[]` is present for every net, so a consumer never has to
    # special-case pin count.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 2)
    output = tmp_path / "two_pin_legs.gds"
    report = compose(
        _gate_rail_request(
            pdk_root,
            reports,
            [{"block": f"b{i}", "port": "U0_G"} for i in (1, 2)],
            output,
            "two_pin_legs_0",
        )
    )

    net = report["nets"][0]
    assert net["routed"] is True
    assert len(net["legs"]) == 1
    assert net["legs"][0]["pins"] == net["pins"]
    assert net["legs"][0]["route_length_um"] == pytest.approx(net["route_length_um"])
    assert net["legs"][0]["reason"] is None


def test_compose_bundle_net_needs_no_leg_for_a_pin_listed_twice(tmp_path, pdk_root):
    # A duplicated {block, port} is the same physical point, so it needs no
    # leg of its own -- the net still routes with the same 2 legs a 3-pin
    # declaration would use, not a zero-length route to itself.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 3)
    output = tmp_path / "dup_pin.gds"
    report = compose(
        _gate_rail_request(
            pdk_root,
            reports,
            [{"block": f"b{i}", "port": "U0_G"} for i in (1, 2, 3, 2)],
            output,
            "dup_pin_0",
        )
    )

    assert report["unrouted_nets"] == []
    assert len(report["nets"][0]["legs"]) == 2


def test_compose_requires_routing_spec_when_connectivity_present(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    with pytest.raises(GenComposeError, match="routing.width_um"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": r1},
                    {"id": "b2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["b1", "b2"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "b1", "port": "P2"},
                            {"block": "b2", "port": "P1"},
                        ],
                    }
                ],
                "routing": {"layer_role": "metal"},  # width_um missing
                "options": {"output": str(tmp_path / "x.gds")},
            }
        )


def test_compose_declare_only_when_routing_absent(tmp_path, pdk_root):
    # #1188: a non-empty connectivity[] with no `routing` key at all is a
    # declare-only request -- validated (the pins reference real ports, as
    # the unknown-block/unknown-port tests above already exercise without a
    # `routing` key), but not routed: no GenComposeError, no metal drawn.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "declare.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "options": {"cell_name": "declare_0", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["unrouted_nets"] == ["N1"]
    net = report["nets"][0]
    assert net["net"] == "N1"
    assert net["routed"] is False
    assert net["status"] == "unrouted"
    assert net["route_length_um"] is None
    assert len(net["legs"]) == 1
    leg = net["legs"][0]
    assert leg["routed"] is False
    assert leg["route_length_um"] is None
    assert leg["reason"] == "routing not requested"
    assert any(
        "N1" in note and "routing not requested" in note
        for note in report["drc_hints"]["notes"]
    )
    # #1198: a declare-only request never draws metal, so `min_spacing_um`
    # must stay null (not the row's own spacing_um) even though
    # `connectivity[]` is non-empty and placement is "row".
    assert report["drc_hints"]["min_spacing_um"] is None


def test_compose_declare_only_when_routing_is_empty_object(tmp_path, pdk_root):
    # Same as above, but `routing: {}` explicitly rather than omitted --
    # both spellings of "no routing requested" behave identically (#1188).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "declare_empty.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {},
            "options": {"cell_name": "declare_empty_0", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == ["N1"]
    net = report["nets"][0]
    assert net["status"] == "unrouted"
    assert net["legs"][0]["reason"] == "routing not requested"
    # #1198: same null-min_spacing_um requirement as the `routing` omitted
    # form above -- `routing: {}` is the same "nothing routed" case.
    assert report["drc_hints"]["min_spacing_um"] is None


def test_compose_declare_only_still_validates_connectivity_pins(tmp_path, pdk_root):
    # Declare-only does not relax the {block, port} check -- a typo'd/stale
    # port name is still a hard failure (exit 1), the whole point of keeping
    # validation independent of routing (#1188).
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="unknown port"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r", "generator_report": block}],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "r", "port": "NOPE"},
                            {"block": "r", "port": "P2"},
                        ],
                    }
                ],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_declare_only_reports_every_leg_of_bundle_net(tmp_path, pdk_root):
    # A >2-pin net still spans as a minimal tree of pin-pairs (#1073's shape),
    # but declare-only reports every one of those legs unrouted -- uniformly,
    # not just the legs that would have failed geometrically under routing.
    reports = _gate_rail_blocks(tmp_path, pdk_root, 3)
    output = tmp_path / "declare_bundle.gds"
    request = _gate_rail_request(
        pdk_root,
        reports,
        [{"block": f"b{i}", "port": "U0_G"} for i in (1, 2, 3)],
        output,
        "declare_bundle_0",
    )
    del request["routing"]
    report = compose(request)

    assert report["unrouted_nets"] == ["VBIAS"]
    net = report["nets"][0]
    assert net["status"] == "unrouted"
    assert net["routed"] is False
    assert net["route_length_um"] is None
    assert len(net["legs"]) == 2  # 3 pins span with 2 legs, same as #1073
    assert all(leg["routed"] is False for leg in net["legs"])
    assert all(leg["reason"] == "routing not requested" for leg in net["legs"])
    assert all(leg["route_length_um"] is None for leg in net["legs"])


def test_compose_declare_only_reports_geometrically_routable_net_unrouted_too(
    tmp_path, pdk_root
):
    # Edge case from #1188's test plan: a net that *would* route cleanly if
    # `routing` were supplied still lands in unrouted_nets[] uniformly under
    # declare-only, with the "not requested" reason rather than a routed
    # result -- declare-only is never "route what's easy, skip what isn't".
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "declare_routable.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "EASY",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "options": {"cell_name": "declare_routable_0", "output": str(output)},
        }
    )

    # Confirm this same net *would* route cleanly with routing supplied, to
    # make the "declare-only never routes it anyway" assertion meaningful.
    routed_output = tmp_path / "declare_routable_control.gds"
    control = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "EASY",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {
                "cell_name": "declare_routable_1",
                "output": str(routed_output),
            },
        }
    )
    assert control["unrouted_nets"] == []

    assert report["unrouted_nets"] == ["EASY"]
    assert report["nets"][0]["status"] == "unrouted"
    assert report["nets"][0]["legs"][0]["reason"] == "routing not requested"


def test_compose_reports_matched_groups_from_input_blocks(tmp_path, pdk_root):
    # mos_array carries a matched_group_id; resistor_strip does not. The
    # composition echoes only the distinct non-null ids it saw.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "m1", rows=1, cols=2)
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "matched.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "arr", "generator_report": m1},
                {"id": "res", "generator_report": r1},
            ],
            "placement": {
                "strategy": "row",
                "order": ["arr", "res"],
                "spacing_um": 1.0,
            },
            "options": {"cell_name": "matched_0", "output": str(output)},
        }
    )
    groups = report["drc_hints"]["matched_groups"]
    assert len(groups) == 1
    assert groups[0]["matched_group_id"] == m1["drc_hints"]["matched_group_id"]
    assert groups[0]["blocks"] == ["arr"]
    assert groups[0]["placement_symmetric"] is None


def test_compose_integration_three_real_blocks_with_connectivity(tmp_path, pdk_root):
    # The #164 5T OTA shape: a differential pair + a current-mirror-labelled
    # load + a tail device, placed in a row and wired with two 2-pin nets.
    # add_guard_ring=False and an *opposite*-facing port pair (diffpair's east
    # -facing _D to mirror's west-facing _S) are both required for a clean
    # route at this phase -- see #199 (a same-facing pair, or an inbound route
    # into a guard-ringed block, produces a spurious device-level short and is
    # now correctly reported unroutable instead; exercised directly by
    # test_compose_route_two_pin_rejects_same_facing_port_pair and
    # test_compose_route_two_pin_rejects_route_into_guard_ringed_block below).
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", "dp", add_guard_ring=False)
    mir = _gen_block(
        tmp_path, pdk_root, "diff_pair", "mir", mirror=True, add_guard_ring=False
    )
    tail = _gen_block(tmp_path, pdk_root, "mos_array", "tail", rows=1, cols=1)

    dp_port = "Q1_1_D"  # faces east -- toward `mirror`, placed to diffpair's east
    mir_port = "M1_1_S"  # faces west -- toward `diffpair`

    output = tmp_path / "ota.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "diffpair", "generator_report": dp},
                {"id": "mirror", "generator_report": mir},
                {"id": "tail", "generator_report": tail},
            ],
            "placement": {
                "strategy": "row",
                "order": ["diffpair", "mirror", "tail"],
                "spacing_um": 2.0,
            },
            "connectivity": [
                {
                    "net": "VOUT",
                    "pins": [
                        {"block": "diffpair", "port": dp_port},
                        {"block": "mirror", "port": mir_port},
                    ],
                },
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "ota_top_0", "output": str(output)},
        }
    )
    assert output.is_file()
    assert len(report["blocks"]) == 3
    assert len(report["nets"]) == 1
    # The VOUT net routes end-to-end (adjacent blocks, ample 2um channel).
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0
    assert report["unrouted_nets"] == []
    # Every generated block placed; the composed cell exists in the GDS.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    assert layout.cell("ota_top_0") is not None


def test_compose_labeled_net_survives_extraction_as_named_pin(tmp_path, pdk_root):
    # #200's own acceptance bar: run the composed output through `klt
    # extract`'s real pin-promotion path and confirm the routed
    # connectivity[] net (VOUT) comes back as a *named* .SUBCKT pin --
    # not just the deck's always-present `vsubs` substrate tie, and not an
    # anonymous `$N` net.
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", "dp", add_guard_ring=False)
    mir = _gen_block(
        tmp_path, pdk_root, "diff_pair", "mir", mirror=True, add_guard_ring=False
    )
    tail = _gen_block(tmp_path, pdk_root, "mos_array", "tail", rows=1, cols=1)

    output = tmp_path / "ota_extract.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "diffpair", "generator_report": dp},
                {"id": "mirror", "generator_report": mir},
                {"id": "tail", "generator_report": tail},
            ],
            "placement": {
                "strategy": "row",
                "order": ["diffpair", "mirror", "tail"],
                "spacing_um": 2.0,
            },
            "connectivity": [
                {
                    "net": "VOUT",
                    "pins": [
                        {"block": "diffpair", "port": "Q1_1_D"},
                        {"block": "mirror", "port": "M1_1_S"},
                    ],
                },
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "ota_top_0", "output": str(output)},
        }
    )
    assert report["nets"][0]["routed"] is True

    result = extract.run_extract(str(output), "sky130", top="ota_top_0")
    pin_names = {net["name"] for net in result["nets"] if net["pin"]}
    # Before #200: pin_names == {"vsubs"} only (VOUT demoted to an anonymous
    # $N during Netlist.purge()). After #200: VOUT survives as a real pin
    # alongside vsubs.
    assert "VOUT" in pin_names
    assert "vsubs" in pin_names


# --------------------------------------------------------------------------- #
# Obstacle-aware routing (#199): same-facing port pairs and guard-ringed
# blocks must no longer report `routed: true` when the backbone would
# actually short a device -- both must show up as `unrouted_nets[]` with an
# explanatory `drc_hints.notes[]` entry instead.
# --------------------------------------------------------------------------- #


def _diffpair_request(dp, mir, *, dp_port, mir_port, pdk_root, spacing_um=1.0):
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "diffpair", "generator_report": dp},
            {"id": "mirror", "generator_report": mir},
        ],
        "placement": {
            "strategy": "row",
            "order": ["diffpair", "mirror"],
            "spacing_um": spacing_um,
        },
        "connectivity": [
            {
                "net": "N1",
                "pins": [
                    {"block": "diffpair", "port": dp_port},
                    {"block": "mirror", "port": mir_port},
                ],
            }
        ],
        "routing": {"layer_role": "metal", "width_um": 0.17},
    }


def test_compose_rejects_same_facing_port_pair(tmp_path, pdk_root):
    # #199 case 1's minimal repro: two _D ports (both direction_deg: 0) on
    # adjacent blocks. The backbone would have to cross the *destination*
    # block's full width to reach its far-side _D pin, plowing straight
    # through that device's own _S pin on the way -- a device-level short
    # `klt extract` would otherwise catch only after the fact.
    dp = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "dp4",
        mirror=False,
        splits=1,
        add_guard_ring=False,
    )
    mir = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "mir4",
        mirror=True,
        splits=1,
        add_guard_ring=False,
    )
    output = tmp_path / "test4.gds"
    request = _diffpair_request(
        dp, mir, dp_port="Q1_1_D", mir_port="M1_1_D", pdk_root=pdk_root
    )
    request["options"] = {"cell_name": "test4", "output": str(output)}
    report = compose(request)

    # Partial success -- blocks still placed, but the net is not routed.
    assert output.is_file()
    assert report["unrouted_nets"] == ["N1"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None
    assert any(
        "N1" in note and "mirror" in note for note in report["drc_hints"]["notes"]
    )


def test_compose_rejects_same_facing_port_pair_across_a_third_block(tmp_path, pdk_root):
    # The same same-facing short, but with the two same-facing blocks not
    # adjacent -- the backbone also has to cross clean through the
    # in-between block's bbox, exercising the third-party-block branch of
    # the obstacle-overlap check (not just "the two connected blocks'"
    # own-block branch).
    dp = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "dp5",
        mirror=False,
        splits=1,
        add_guard_ring=False,
    )
    mid = _gen_block(tmp_path, pdk_root, "mos_array", "mid5", rows=1, cols=1)
    mir = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "mir5",
        mirror=True,
        splits=1,
        add_guard_ring=False,
    )
    output = tmp_path / "test5.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "diffpair", "generator_report": dp},
                {"id": "mid", "generator_report": mid},
                {"id": "mirror", "generator_report": mir},
            ],
            "placement": {
                "strategy": "row",
                "order": ["diffpair", "mid", "mirror"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "diffpair", "port": "Q1_1_D"},
                        {"block": "mirror", "port": "M1_1_D"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "test5", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["unrouted_nets"] == ["N1"]
    assert report["nets"][0]["routed"] is False


# --------------------------------------------------------------------------- #
# route_two_pin() waypoints_um (#634) -- caller-supplied route-around for a
# same-facing port pair the fixed-shape backbone above cannot reach. Uses
# hand-built block/port dicts (no real generator/gds involved) since
# route_two_pin() never reads a block's drawn geometry for a cross-block net.
# --------------------------------------------------------------------------- #


def _same_facing_pair_fixture():
    """Two blocks in a row, each with a single west-facing port, reproducing
    this issue's generic repro: block a's Y (west) feeds block b's A (west),
    but b sits *east* of a, so the fixed one-jog backbone collapses to a
    straight line straight through a's own interior (see the
    test_manhattan_backbone_waypoints_route_through_supplied_points_in_order
    unit test above for the geometry)."""
    blocks = {
        "a": {
            "id": "a",
            "port_names": {"Y"},
            "ports": {"Y": {"x_um": 0.0, "y_um": 5.0, "direction_deg": 180}},
        },
        "b": {
            "id": "b",
            "port_names": {"A"},
            "ports": {"A": {"x_um": 12.0, "y_um": 5.0, "direction_deg": 180}},
        },
    }
    offsets = {"a": {"x": 0.0, "y": 0.0}, "b": {"x": 0.0, "y": 0.0}}
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "b": {"x0": 12.0, "y0": 0.0, "x1": 22.0, "y1": 10.0},
    }
    pin_a = {"block": "a", "port": "Y"}
    pin_b = {"block": "b", "port": "A"}
    return blocks, offsets, bboxes, pin_a, pin_b


def test_route_two_pin_rejects_same_facing_pair_without_waypoints():
    # Baseline: omitting waypoints_um entirely preserves today's rejection
    # (the fixed backbone plows straight through block a's interior).
    blocks, offsets, bboxes, pin_a, pin_b = _same_facing_pair_fixture()
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is False
    assert result["points_um"] is None


def test_route_two_pin_routes_same_facing_pair_with_a_clearing_waypoint():
    # A waypoint pair that lifts the jog above both blocks' shared bbox top
    # (y=10) clears the obstacle entirely -- routed: true, same as any other
    # backbone that passes every check.
    blocks, offsets, bboxes, pin_a, pin_b = _same_facing_pair_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.3,
        waypoints_um=[(-0.3, 11.0), (11.7, 11.0)],
    )
    assert result["routed"] is True
    assert result["reason"] is None
    assert result["points_um"][0] == (0.0, 5.0)
    assert result["points_um"][-1] == (12.0, 5.0)
    assert result["route_length_um"] is not None


def test_route_two_pin_rejects_width_inflated_near_miss_against_a_third_block():
    # #999: the obstacle-overlap check used to test the backbone's
    # zero-width *centerline* against every other placed block's bbox --
    # ignoring routing.width_um, the width of the metal actually drawn. A
    # centerline that clears a block's bbox edge by less than width_um / 2
    # still draws metal on top of that block (the conductor extends
    # width_um / 2 past the centerline on every side), a silent short
    # neither this check nor `klt drc` caught (see issue body).
    #
    # Reuses the same-facing-pair fixture (both ports facing west, #199's
    # own repro shape) with a caller-supplied waypoint path that clears both
    # connected blocks' own bboxes cleanly (mirroring
    # test_route_two_pin_routes_same_facing_pair_with_a_clearing_waypoint),
    # plus one more placed block ("obstacle") sitting just 0.2um below the
    # jog's y=11 crossing -- less than half of the 0.5um route width (0.25),
    # so the drawn conductor (spanning y=10.75..11.25) clips 0.05um into
    # the obstacle's true bbox (y0=11.2) even though the centerline itself
    # never enters it.
    blocks, offsets, bboxes, pin_a, pin_b = _same_facing_pair_fixture()
    bboxes = {
        **bboxes,
        "obstacle": {"x0": 4.0, "y0": 11.2, "x1": 8.0, "y1": 20.0},
    }
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.5,
        waypoints_um=[(-0.5, 11.0), (11.5, 11.0)],
    )
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "block 'obstacle'" in result["reason"]


def test_route_two_pin_still_routes_the_same_waypoint_path_without_the_obstacle():
    # Baseline for the regression above: the identical waypoint path, minus
    # the "obstacle" block, still routes cleanly -- confirms the rejection
    # above is caused specifically by the width-inflated near miss against
    # "obstacle", not by blocks "a"/"b" (whose own approach margins are
    # unaffected by the width_um / 2 bbox inflation -- see the allowances_um
    # compensation in route_two_pin's obstacle-overlap check).
    blocks, offsets, bboxes, pin_a, pin_b = _same_facing_pair_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.5,
        waypoints_um=[(-0.5, 11.0), (11.5, 11.0)],
    )
    assert result["routed"] is True
    assert result["reason"] is None
    assert result["route_length_um"] is not None


def test_route_two_pin_still_rejects_a_waypoint_that_crosses_a_block():
    # The obstacle-overlap check (#199 case 1) is not bypassed just because
    # the caller supplied a waypoint -- one that plows straight through
    # block a's own interior is rejected exactly like the no-waypoint case.
    blocks, offsets, bboxes, pin_a, pin_b = _same_facing_pair_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.3,
        waypoints_um=[(5.0, 5.0)],
    )
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "block 'a'" in result["reason"]


def _narrow_gap_pair_fixture():
    """Two blocks packed closer together than the route width, with their
    ports on the row's *outer* edges at different y.

    This is the shape the channel-width routability heuristic (check 1) fires
    on: both ports face x, their y differs (so the fixed one-jog backbone
    needs a vertical jog), and the only gap between the two bboxes is 0.1um
    -- narrower than the 0.3um route. Both ports sit exactly on their own
    block's outer edge, so an approach from outside the row crosses neither
    block's interior at all.
    """
    blocks = {
        "a": {
            "id": "a",
            "port_names": {"Y"},
            "ports": {"Y": {"x_um": 0.0, "y_um": 5.0, "direction_deg": 180}},
        },
        "b": {
            "id": "b",
            "port_names": {"A"},
            "ports": {"A": {"x_um": 20.1, "y_um": 6.0, "direction_deg": 0}},
        },
    }
    offsets = {"a": {"x": 0.0, "y": 0.0}, "b": {"x": 0.0, "y": 0.0}}
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        # 0.1um channel between the two blocks -- narrower than width_um=0.3.
        "b": {"x0": 10.1, "y0": 0.0, "x1": 20.1, "y1": 10.0},
    }
    pin_a = {"block": "a", "port": "Y"}
    pin_b = {"block": "b", "port": "A"}
    return blocks, offsets, bboxes, pin_a, pin_b


def test_route_two_pin_narrow_channel_still_rejected_without_waypoints():
    # Baseline for the regression below: with no waypoints_um the fixed
    # one-jog backbone really does have to squeeze its vertical jog through
    # the 0.1um channel, so check 1 rejects -- unchanged behaviour.
    blocks, offsets, bboxes, pin_a, pin_b = _narrow_gap_pair_fixture()
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "vertical jog needs a channel" in result["reason"]


def test_route_two_pin_waypoints_bypass_the_narrow_channel_heuristic():
    # Regression (#634 review): the channel-width heuristic used to run
    # unconditionally, *before* manhattan_backbone() was even called, and
    # reasoned about a direct jog between the raw port positions rather than
    # about the path actually drawn. So a caller who supplied waypoints_um
    # routing entirely over the top of the row -- never going anywhere near
    # the 0.1um channel -- still got
    #   routed: False, "vertical jog needs a channel >= width 0.3um ..."
    # which silently defeated waypoints_um in precisely the tightly-packed
    # case (gap < route width) that most needs a caller-supplied route-around.
    blocks, offsets, bboxes, pin_a, pin_b = _narrow_gap_pair_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.3,
        # Up the west side of block a, straight across above both blocks'
        # shared bbox top (y=10), then down the east side of block b.
        waypoints_um=[(-0.3, 11.0), (20.4, 11.0)],
    )
    assert result["routed"] is True
    assert result["reason"] is None
    assert result["points_um"][0] == (0.0, 5.0)
    assert result["points_um"][-1] == (20.1, 6.0)
    assert result["route_length_um"] is not None
    # The drawn path never enters the narrow channel it was rejected over.
    assert not any(10.0 < x < 10.1 for x, _ in result["points_um"])


def _self_net_same_row_fixture():
    """One block with three same-row, same-facing (north) ports on the route
    layer -- the #453 degenerate-jog shape: bussing E0 to E1 with B0 sitting
    between them."""
    layer = {"layer": 68, "datatype": 20}
    blocks = {
        "u": {
            "id": "u",
            "port_names": {"E0", "B0", "E1"},
            "ports": {
                "E0": {
                    "x_um": 0.0,
                    "y_um": 10.0,
                    "direction_deg": 90,
                    "width_um": 0.22,
                    "layer": layer,
                },
                "B0": {
                    "x_um": 5.0,
                    "y_um": 10.0,
                    "direction_deg": 90,
                    "width_um": 0.22,
                    "layer": layer,
                },
                "E1": {
                    "x_um": 10.0,
                    "y_um": 10.0,
                    "direction_deg": 90,
                    "width_um": 0.22,
                    "layer": layer,
                },
            },
        }
    }
    offsets = {"u": {"x": 0.0, "y": 0.0}}
    bboxes = {"u": {"x0": -1.0, "y0": 0.0, "x1": 11.0, "y1": 10.0}}
    pin_a = {"block": "u", "port": "E0"}
    pin_b = {"block": "u", "port": "E1"}
    return blocks, offsets, bboxes, pin_a, pin_b


def test_route_two_pin_self_net_same_row_still_rejected_without_waypoints():
    # Baseline: #453's conservative same-direction fallback still rejects the
    # degenerate single-jog backbone that busses E0 to E1 straight over B0.
    blocks, offsets, bboxes, pin_a, pin_b = _self_net_same_row_fixture()
    result = gen_compose.route_two_pin(
        pin_a, pin_b, blocks, offsets, bboxes, 0.3, route_layer=(68, 20)
    )
    assert result["routed"] is False
    assert "'B0'" in result["reason"]


def test_route_two_pin_waypoints_bypass_the_same_row_degenerate_jog_check():
    # Regression (#634 review, same defect class as the channel-width check):
    # #453's conservative fallback rejects on the raw port positions alone,
    # justified *only* by manhattan_backbone() collapsing to a single jog
    # lifted one stub width along the ports' own row. Supplying waypoints_um
    # replaces that shape entirely, so a caller lifting the bus well clear of
    # B0's pad must not be rejected by a prediction about a path that is
    # never drawn.
    blocks, offsets, bboxes, pin_a, pin_b = _self_net_same_row_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.3,
        route_layer=(68, 20),
        waypoints_um=[(0.0, 12.0), (10.0, 12.0)],
    )
    assert result["routed"] is True
    assert result["reason"] is None
    assert result["route_length_um"] is not None


def test_route_two_pin_waypoints_do_not_disable_the_self_net_pad_check():
    # ...but the pad-footprint test (check 3 proper) measures the *drawn*
    # points, so waypoints that really do run across B0's inflated footprint
    # are still rejected. Skipping the fixed-shape fallback above must not be
    # mistaken for skipping the geometry check.
    blocks, offsets, bboxes, pin_a, pin_b = _self_net_same_row_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.3,
        route_layer=(68, 20),
        waypoints_um=[(0.0, 10.1), (10.0, 10.1)],
    )
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "'B0'" in result["reason"]


# --------------------------------------------------------------------------- #
# route_two_pin() bounded detour search (#1167) -- a backbone rejected *only*
# for crossing unrelated blocks' bboxes now retries around them on up to two
# alternate lanes before the net is reported unroutable. Hand-built block/port
# dicts again (no generator/gds involved): route_two_pin() reads nothing but
# the reported bbox/ports for a cross-block net.
# --------------------------------------------------------------------------- #


def _row_obstacle_fixture(obstacles=None, extra_bboxes=None):
    """Blocks in a row: the outer two carry the net's ports (facing each
    other), everything in between is an unrelated obstacle sitting squarely on
    the straight-line path between them.

    This is #1164's root cause #2 in miniature -- the shape that made *every*
    net except one between immediately-adjacent blocks unroutable, since the
    fixed one-jog backbone runs straight through whatever the row puts in
    between.
    """
    obstacles = obstacles or {"m": {"x0": 12.0, "y0": 0.0, "x1": 22.0, "y1": 10.0}}
    blocks = {
        "a": {
            "id": "a",
            "port_names": {"Y"},
            "ports": {"Y": {"x_um": 10.0, "y_um": 5.0, "direction_deg": 0}},
        },
        "b": {
            "id": "b",
            "port_names": {"A"},
            "ports": {"A": {"x_um": 24.0, "y_um": 5.0, "direction_deg": 180}},
        },
    }
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "b": {"x0": 24.0, "y0": 0.0, "x1": 34.0, "y1": 10.0},
        **obstacles,
        **(extra_bboxes or {}),
    }
    for block_id in (*obstacles, *(extra_bboxes or {})):
        blocks[block_id] = {"id": block_id, "port_names": set(), "ports": {}}
    offsets = {block_id: {"x": 0.0, "y": 0.0} for block_id in blocks}
    pin_a = {"block": "a", "port": "Y"}
    pin_b = {"block": "b", "port": "A"}
    return blocks, offsets, bboxes, pin_a, pin_b


def _crossing_um(points, bbox_um, width_um):
    """How much of ``points`` runs inside ``bbox_um`` -- inflated by half the
    route width, exactly as route_two_pin's own obstacle check inflates it."""
    half = width_um / 2.0
    inflated = {
        "x0": bbox_um["x0"] - half,
        "y0": bbox_um["y0"] - half,
        "x1": bbox_um["x1"] + half,
        "y1": bbox_um["y1"] + half,
    }
    return sum(
        gen_compose._segment_bbox_interior_overlap_um(p0, p1, inflated)
        for p0, p1 in zip(points, points[1:], strict=False)
    )


def test_route_two_pin_detours_around_an_unrelated_block_in_the_row():
    # #1167: the two ports are not immediate row-neighbours -- block 'm' sits
    # between them -- so the fixed one-jog backbone plows straight through
    # m's bbox and used to be rejected outright ("crosses ... through
    # unrelated block 'm''s bbox"). The router now jogs around m instead.
    blocks, offsets, bboxes, pin_a, pin_b = _row_obstacle_fixture()
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is True, result["reason"]
    assert result["reason"] is None
    points = result["points_um"]
    # Endpoints are still the two ports themselves...
    assert points[0] == (10.0, 5.0)
    assert points[-1] == (24.0, 5.0)
    # ...and no part of the drawn path enters the obstacle (the same
    # width-inflated measure the check itself applies).
    assert _crossing_um(points, bboxes["m"], 0.3) == pytest.approx(0.0)
    # The detour lane clears the obstacle by more than the width_um / 2 the
    # overlap check bottoms out at -- it is drawn clear, not legal-by-a-hair.
    lane_y = max(y for _, y in points)
    assert lane_y >= bboxes["m"]["y1"] + 0.3
    # It is a detour, not a shortcut: longer than the straight-line distance.
    assert result["route_length_um"] > 14.0


def test_route_two_pin_detour_takes_the_shorter_of_the_two_lanes():
    # The obstacle is tall (y1 = 30) but its bottom is level with the row, so
    # going *under* it is much shorter than going over -- the bounded search
    # tries the cheaper lane first, and its result is what gets drawn.
    blocks, offsets, bboxes, pin_a, pin_b = _row_obstacle_fixture(
        obstacles={"m": {"x0": 12.0, "y0": 0.0, "x1": 22.0, "y1": 30.0}}
    )
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is True, result["reason"]
    assert _crossing_um(result["points_um"], bboxes["m"], 0.3) == pytest.approx(0.0)
    assert min(y for _, y in result["points_um"]) <= bboxes["m"]["y0"] - 0.3


def test_route_two_pin_detours_around_two_obstacles_on_one_lane():
    # Documented depth limit (#1167): what the search bounds is the number of
    # *lanes* it tries (two), not the number of blocks in the way -- one lane
    # is placed clear of every block it spans, so a pair of obstacles between
    # the pins routes on the same single detour.
    blocks, offsets, bboxes, pin_a, pin_b = _row_obstacle_fixture(
        obstacles={
            "m1": {"x0": 12.0, "y0": 0.0, "x1": 16.0, "y1": 10.0},
            "m2": {"x0": 18.0, "y0": -1.0, "x1": 22.0, "y1": 12.0},
        }
    )
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is True, result["reason"]
    for obstacle in ("m1", "m2"):
        assert _crossing_um(
            result["points_um"], bboxes[obstacle], 0.3
        ) == pytest.approx(0.0)
    # The lane it takes (under, the shorter side here) is placed clear of the
    # *union* of both obstacles -- m2's deeper y0, not just the first one's.
    assert min(y for _, y in result["points_um"]) == pytest.approx(-1.6)


def test_route_two_pin_still_rejects_when_both_detour_lanes_are_blocked():
    # Edge case from the issue's test plan: an obstacle the bounded search
    # cannot get around -- here two further blocks straddle the lane's own
    # exit column, above and below the row -- still reports the net
    # unroutable, naming the crossed block and the detour that was tried.
    blocks, offsets, bboxes, pin_a, pin_b = _row_obstacle_fixture(
        extra_bboxes={
            "north": {"x0": 10.5, "y0": 12.0, "x1": 10.7, "y1": 14.0},
            "south": {"x0": 10.5, "y0": -5.0, "x1": 10.7, "y1": -2.0},
        }
    )
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "block 'm'" in result["reason"]
    assert "detour" in result["reason"]


def test_route_two_pin_does_not_detour_around_its_own_pin_s_block():
    # Scope guard: the detour search fires only for *unrelated* blocks. A
    # backbone that plows through one of its own two pins' blocks (the
    # same-facing port pair #634 exists for) is still rejected, and still
    # points the caller at waypoints_um rather than silently rerouting.
    blocks, offsets, bboxes, pin_a, pin_b = _same_facing_pair_fixture()
    result = gen_compose.route_two_pin(pin_a, pin_b, blocks, offsets, bboxes, 0.3)
    assert result["routed"] is False
    assert "block 'a'" in result["reason"]
    assert "detour" not in result["reason"]


def test_route_two_pin_caller_waypoints_are_never_second_guessed():
    # A caller who supplies waypoints_um owns the path: a supplied path that
    # crosses an unrelated block is reported unroutable exactly as before
    # #1167, rather than being silently replaced by a detour of the router's
    # own choosing.
    blocks, offsets, bboxes, pin_a, pin_b = _row_obstacle_fixture()
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.3,
        waypoints_um=[(17.0, 5.0)],
    )
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "block 'm'" in result["reason"]
    assert "detour" not in result["reason"]


def _self_net_opposite_facing_ports_with_obstacle_fixture():
    """A same-block self-net (#1179) whose two ports face *toward* each
    other -- ``L`` at x=8 facing east (``+x``), ``R`` at x=12 facing west
    (``-x``) -- with an unrelated block ``m`` sitting on the straight path
    between them.

    Because both ports belong to the same block ``u``, ``_detour_escape_um``
    receives the *same* ``bbox_um`` for both endpoints; each port's own
    facing direction then pushes its escape point toward the *opposite*
    edge of that shared bbox (``L`` toward ``u``'s east edge at x=20, ``R``
    toward ``u``'s west edge at x=0) rather than toward the near edge past
    the obstacle. This is the suspected-cause shape from the issue: distinct
    from ``_self_net_same_row_fixture`` (:func:`_self_net_same_row_fixture`),
    whose ports face north (perpendicular to the row) and so never exercise
    the bbox-edge push at all.
    """
    layer = {"layer": 68, "datatype": 20}
    blocks = {
        "u": {
            "id": "u",
            "port_names": {"L", "R"},
            "ports": {
                "L": {
                    "x_um": 8.0,
                    "y_um": 5.0,
                    "direction_deg": 0,
                    "width_um": 0.3,
                    "layer": layer,
                },
                "R": {
                    "x_um": 12.0,
                    "y_um": 5.0,
                    "direction_deg": 180,
                    "width_um": 0.3,
                    "layer": layer,
                },
            },
        },
        "m": {"id": "m", "port_names": set(), "ports": {}},
    }
    offsets = {"u": {"x": 0.0, "y": 0.0}, "m": {"x": 0.0, "y": 0.0}}
    bboxes = {
        "u": {"x0": 0.0, "y0": 0.0, "x1": 20.0, "y1": 10.0},
        "m": {"x0": 9.0, "y0": 4.0, "x1": 11.0, "y1": 6.0},
    }
    pin_a = {"block": "u", "port": "L"}
    pin_b = {"block": "u", "port": "R"}
    return blocks, offsets, bboxes, pin_a, pin_b


def test_route_two_pin_self_net_opposite_facing_detour_rejects_cleanly():
    # #1179: a same-block self-net whose two ports face toward each other
    # triggers the bounded detour search (an unrelated block sits between
    # them), but the mis-derived escape points (both computed from the one
    # shared bbox, pushed to opposite edges of it -- see the fixture
    # docstring) make each candidate lane's own entry/exit leg run straight
    # back across the obstacle at the ports' own y-level instead of clearing
    # it. Confirms the "fails safe" hypothesis: the malformed lane is still
    # caught by the same obstacle-overlap check every other path goes
    # through, so the net is reported unroutable with no shapes drawn --
    # never a silently bad path.
    blocks, offsets, bboxes, pin_a, pin_b = (
        _self_net_opposite_facing_ports_with_obstacle_fixture()
    )
    result = gen_compose.route_two_pin(
        pin_a, pin_b, blocks, offsets, bboxes, 0.3, route_layer=(68, 20)
    )
    assert result["routed"] is False
    assert result["points_um"] is None
    assert "block 'm'" in result["reason"]
    assert "detour" in result["reason"]


def _self_net_away_facing_ports_with_obstacle_fixture():
    """A same-block self-net (#1179) whose two ports face *away* from each
    other -- ``L`` at x=8 facing west (``-x``), ``R`` at x=12 facing east
    (``+x``) -- with an unrelated block ``m`` sitting on the straight path
    between them. This is the same geometry as
    :func:`_self_net_opposite_facing_ports_with_obstacle_fixture`, with only
    the two ports' ``direction_deg`` swapped.

    Here the bbox-edge push (see :func:`_detour_escape_um`'s docstring)
    sends each escape point *away* from the other port instead of past it,
    so the entry/exit legs never overlap the original backbone's range --
    the detour lane draws a full loop around the whole block and the net
    routes successfully, rather than being rejected as in the
    facing-each-other orientation.
    """
    layer = {"layer": 68, "datatype": 20}
    blocks = {
        "u": {
            "id": "u",
            "port_names": {"L", "R"},
            "ports": {
                "L": {
                    "x_um": 8.0,
                    "y_um": 5.0,
                    "direction_deg": 180,
                    "width_um": 0.3,
                    "layer": layer,
                },
                "R": {
                    "x_um": 12.0,
                    "y_um": 5.0,
                    "direction_deg": 0,
                    "width_um": 0.3,
                    "layer": layer,
                },
            },
        },
        "m": {"id": "m", "port_names": set(), "ports": {}},
    }
    offsets = {"u": {"x": 0.0, "y": 0.0}, "m": {"x": 0.0, "y": 0.0}}
    bboxes = {
        "u": {"x0": 0.0, "y0": 0.0, "x1": 20.0, "y1": 10.0},
        "m": {"x0": 9.0, "y0": 4.0, "x1": 11.0, "y1": 6.0},
    }
    pin_a = {"block": "u", "port": "L"}
    pin_b = {"block": "u", "port": "R"}
    return blocks, offsets, bboxes, pin_a, pin_b


def test_route_two_pin_self_net_away_facing_detour_loops_around_cleanly():
    # #1179 follow-up: swapping the two ports' facing direction from "toward
    # each other" to "away from each other" flips the bbox-edge push from
    # "past the other port" to "past the block's own far edge on its own
    # side" -- the escape legs no longer overlap the original backbone's
    # range at all, so the detour lane draws a full loop around the whole
    # block instead of being rejected. Confirms this is a valid, non-crossing
    # (if indirect) path rather than a silently bad one.
    blocks, offsets, bboxes, pin_a, pin_b = (
        _self_net_away_facing_ports_with_obstacle_fixture()
    )
    result = gen_compose.route_two_pin(
        pin_a, pin_b, blocks, offsets, bboxes, 0.3, route_layer=(68, 20)
    )
    assert result["routed"] is True
    points = result["points_um"]
    assert points is not None
    # The loop-around never enters the obstacle's x-range (9..11) at the
    # obstacle's own y-range (4..6): every segment either sits outside
    # x=[9, 11] or outside y=[4, 6].
    obstacle = bboxes["m"]
    for (x0, y0), (x1, y1) in pairwise(points):
        seg_x0, seg_x1 = sorted((x0, x1))
        seg_y0, seg_y1 = sorted((y0, y1))
        crosses_x = seg_x1 > obstacle["x0"] and seg_x0 < obstacle["x1"]
        crosses_y = seg_y1 > obstacle["y0"] and seg_y0 < obstacle["y1"]
        assert not (crosses_x and crosses_y)


def test_compose_routes_around_the_middle_block_of_a_three_block_row(
    tmp_path, pdk_root
):
    # End-to-end (#1167): three resistor strips in a row, wiring the *outer*
    # two together. Their ports face each other across the whole row, so the
    # straight-line backbone runs through the middle block -- the case that
    # used to land in unrouted_nets[]. The composed output must both route and
    # stay DRC-clean, and the drawn metal must not touch the middle block.
    reports = [
        _gen_block(tmp_path, pdk_root, "resistor_strip", f"r{index}")
        for index in range(3)
    ]
    output = tmp_path / "detour.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": f"b{index + 1}", "generator_report": block}
                for index, block in enumerate(reports)
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2", "b3"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P2"},
                        {"block": "b3", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "detour_0", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    net = report["nets"][0]
    assert net["routed"] is True

    # The metal actually written: one li1 path, jogged around the middle
    # block rather than straight through it.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("detour_0")
    paths = [s.path for s in top.shapes(layout.layer(67, 20)).each() if s.is_path()]
    assert len(paths) == 1
    drawn = [(p.x * layout.dbu, p.y * layout.dbu) for p in paths[0].each_point()]
    assert len(drawn) > 2  # a straight span would be two points
    middle_bbox = report["blocks"][1]["bbox_um"]
    assert _crossing_um(drawn, middle_bbox, 0.17) == pytest.approx(0.0)

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    # The loop closes: extraction sees the two outer resistors on one net.
    result = extract.run_extract(str(output), "sky130", top="detour_0")
    assert "N1" in {net["name"] for net in result["nets"] if net["pin"]}


def test_compose_gf180mcu_row_routes_a_non_adjacent_device_pair(
    tmp_path, both_pdk_root
):
    # Approximates #1164's own exercise (root cause #2): a row of one-device
    # `mos_array` groups on gf180mcu, wiring a *non-adjacent* pair -- b1's
    # drain (right edge) to b3's source (left edge), with b2 sitting squarely
    # in between. Every such net in that report came back unrouted with
    # "crosses N um through unrelated block X's bbox"; here it routes, on the
    # second deck, and `klt drc --deck gf180mcu` stays clean.
    reports = [
        _gen_block_variant(
            tmp_path,
            both_pdk_root,
            "gf180mcuD",
            "mos_array",
            f"row_m{index}",
            rows=1,
            cols=1,
            dummy=0,
            gate_contact=True,
        )
        for index in range(3)
    ]
    output = tmp_path / "row_detour_gf180.gds"
    report = compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "blocks": [
                {"id": f"b{index + 1}", "generator_report": block}
                for index, block in enumerate(reports)
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2", "b3"],
                "spacing_um": 2.0,
            },
            "connectivity": [
                {
                    "net": "MID",
                    "pins": [
                        {"block": "b1", "port": "U0_D"},
                        {"block": "b3", "port": "U0_S"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.38},
            "options": {"cell_name": "row_detour", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True

    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_compose_route_two_pin_waypoints_um_rejects_malformed_entries(
    tmp_path, pdk_root
):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    base_request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": "r", "generator_report": block}],
        "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {"output": str(tmp_path / "out.gds")},
    }

    def _request_with(waypoints_um):
        request = dict(base_request)
        request["connectivity"] = [
            {
                "net": "N1",
                "pins": [{"block": "r", "port": "P1"}, {"block": "r", "port": "P2"}],
                "waypoints_um": waypoints_um,
            }
        ]
        return request

    with pytest.raises(GenComposeError, match="waypoints_um"):
        compose(_request_with([]))  # empty -- must be non-empty when present
    with pytest.raises(GenComposeError, match="waypoints_um"):
        compose(_request_with("not-a-list"))
    with pytest.raises(GenComposeError, match="waypoints_um"):
        compose(_request_with([[1.0]]))  # wrong pair length
    with pytest.raises(GenComposeError, match="waypoints_um"):
        compose(_request_with([["1.0", 2.0]]))  # non-numeric coordinate


def test_compose_routes_same_facing_port_pair_when_waypoints_um_clears_it(
    tmp_path, pdk_root
):
    # End-to-end (#634 acceptance criteria): the exact same-facing-pair shape
    # test_compose_rejects_same_facing_port_pair (P1-to-P1, both west-facing,
    # 180deg) rejects above, now routes when the request's connectivity[]
    # entry supplies waypoints_um clearing the obstacle -- exercising the
    # full JSON-parsing path (_parse_connectivity), not just route_two_pin()
    # directly.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "wr1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "wr2")
    width_um = 0.17
    gap_um = 2.0
    b2_x = r1["bbox_um"]["x1"] + gap_um

    p1_port = next(p for p in r1["ports"] if p["name"] == "P1")
    assert p1_port["direction_deg"] == 180  # west-facing, same as r2's P1

    # sa/sb: each port's own stub end, leaving westward by the route width.
    sa_x = p1_port["x_um"] - width_um
    sb_x = b2_x + p1_port["x_um"] - width_um
    top_y = max(r1["bbox_um"]["y1"], r2["bbox_um"]["y1"]) + 1.0

    def _request(waypoints_um):
        return {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["b1", "b2"],
                "origins_um": {
                    "b1": {"x": 0.0, "y": 0.0},
                    "b2": {"x": b2_x, "y": 0.0},
                },
            },
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "b1", "port": "P1"},
                        {"block": "b2", "port": "P1"},
                    ],
                    **({"waypoints_um": waypoints_um} if waypoints_um else {}),
                }
            ],
            "routing": {"layer_role": "metal", "width_um": width_um},
            "options": {
                "cell_name": "waypoint_test",
                "output": str(tmp_path / "waypoint_test.gds"),
            },
        }

    # Regression: omitting waypoints_um preserves today's rejection.
    without = compose(_request(None))
    assert without["unrouted_nets"] == ["N1"]
    assert without["nets"][0]["routed"] is False

    # With a waypoint pair that clears both blocks' shared bbox top:
    with_waypoints = compose(_request([[sa_x, top_y], [sb_x, top_y]]))
    assert with_waypoints["unrouted_nets"] == []
    assert with_waypoints["nets"][0]["routed"] is True
    assert with_waypoints["nets"][0]["route_length_um"] is not None


def test_compose_rejects_route_into_guard_ringed_block(tmp_path, pdk_root):
    # #199 case 2's minimal repro: `add_guard_ring: true` (diff_pair's
    # default) draws a local-metal ring around the block; any inbound route
    # to a non-tap pin crosses that ring, merging the routed net with the
    # ring's own tap net.
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", "dp_ring1", splits=1)
    mir = _gen_block(
        tmp_path, pdk_root, "diff_pair", "mir_ring1", mirror=True, splits=1
    )
    output = tmp_path / "ring1.gds"
    request = _diffpair_request(
        dp, mir, dp_port="Q1_1_D", mir_port="M1_1_S", pdk_root=pdk_root
    )
    request["options"] = {"cell_name": "ring1", "output": str(output)}
    report = compose(request)

    assert output.is_file()
    assert report["unrouted_nets"] == ["N1"]
    assert report["nets"][0]["routed"] is False
    assert any("ring" in note.lower() for note in report["drc_hints"]["notes"])


def test_compose_rejects_route_out_of_guard_ringed_source_block(tmp_path, pdk_root):
    # The ring check must be symmetric: a guard ring fully encloses its
    # block, so a route is just as unsafe leaving a ring-having *source*
    # block's non-tap pin as it is entering a ring-having *destination*
    # block's non-tap pin (Test Plan's "source block" edge case).
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", "dp_ring2", splits=1)  # ring on
    mir = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "mir_ring2",
        mirror=True,
        splits=1,
        add_guard_ring=False,
    )
    output = tmp_path / "ring2.gds"
    request = _diffpair_request(
        dp, mir, dp_port="Q1_1_D", mir_port="M1_1_S", pdk_root=pdk_root
    )
    request["options"] = {"cell_name": "ring2", "output": str(output)}
    report = compose(request)

    assert report["unrouted_nets"] == ["N1"]
    assert report["nets"][0]["routed"] is False


def test_compose_allows_connecting_directly_to_a_guard_ring_tap_port(
    tmp_path, pdk_root
):
    # A route *to* a ring's own tap port (rather than one of the block's
    # regular device pins) is exactly what the ring's tap ports are for --
    # not a short, so it must still route cleanly.
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", "dp_ring3", splits=1)
    ring = _gen_block(
        tmp_path,
        pdk_root,
        "guard_ring",
        "ring3",
        inner_width_um=2.0,
        inner_height_um=2.0,
    )
    output = tmp_path / "ring3.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "diffpair", "generator_report": dp},
                {"id": "ring", "generator_report": ring},
            ],
            "placement": {
                "strategy": "row",
                "order": ["diffpair", "ring"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "TAPNET",
                    "pins": [
                        {"block": "diffpair", "port": "TAP_E"},
                        {"block": "ring", "port": "TAP_W"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "ring3", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True


def test_compose_allows_opposite_facing_ports_without_guard_ring(tmp_path, pdk_root):
    # No regression: the already-working pattern from #196's bring-up
    # (opposite-facing ports, add_guard_ring: false) must still route.
    dp = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "dp_ok",
        mirror=False,
        splits=1,
        add_guard_ring=False,
    )
    mir = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "mir_ok",
        mirror=True,
        splits=1,
        add_guard_ring=False,
    )
    output = tmp_path / "ok.gds"
    request = _diffpair_request(
        dp, mir, dp_port="Q1_1_D", mir_port="M1_1_S", pdk_root=pdk_root
    )
    request["options"] = {"cell_name": "ok", "output": str(output)}
    report = compose(request)

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0


# --------------------------------------------------------------------------- #
# Self-net pad-crossing (#433): a same-block net (both pins on one block) was
# exempted from the #199 case 1 obstacle-overlap check entirely, since its
# backbone is always inside its own block's bbox -- but that exemption also
# let a self-net's backbone be drawn straight over one of the block's *other*
# pads with no check at all, silently shorting them together on the router's
# one available metal role. `route_two_pin` must instead compare the
# backbone against every other same-layer port on that block, and report the
# net unrouted (never `routed: true`) when it overlaps one.
# --------------------------------------------------------------------------- #


def test_compose_rejects_self_net_that_crosses_another_pad_on_same_block(
    tmp_path, pdk_root
):
    # The exact reproduction from the issue: an 8-unit bjt_array, bussing
    # three emitters (Q0_E, Q1_E, Q2_E) into one node via two 2-pin self-nets
    # chained end to end. Each net's backbone jogs directly over the base pad
    # sitting between the two emitters it connects (Q0_B between Q0_E/Q1_E,
    # Q1_B between Q1_E/Q2_E) -- before #433 this composed `routed: true` and
    # extracted a single 12-terminal net for what should be a 3-terminal bus.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        rows=1,
        cols=8,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    output = tmp_path / "bjt_bus.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "EBUS1",
                    "pins": [
                        {"block": "arr", "port": "Q0_E"},
                        {"block": "arr", "port": "Q1_E"},
                    ],
                },
                {
                    "net": "EBUS2",
                    "pins": [
                        {"block": "arr", "port": "Q1_E"},
                        {"block": "arr", "port": "Q2_E"},
                    ],
                },
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "bjt_bus", "output": str(output)},
        }
    )

    # Partial success -- blocks still placed, but neither bussing net routed.
    assert output.is_file()
    assert report["unrouted_nets"] == ["EBUS1", "EBUS2"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None
    assert report["nets"][1]["routed"] is False
    assert report["nets"][1]["route_length_um"] is None
    assert any(
        "EBUS1" in note and "Q0_B" in note for note in report["drc_hints"]["notes"]
    )
    assert any(
        "EBUS2" in note and "Q1_B" in note for note in report["drc_hints"]["notes"]
    )

    # No metal path was drawn for either net at all -- routed: false means no
    # `routed_geometry[]` entry, so nothing was drawn on li1 for these nets in
    # the first place (the short the issue describes never gets a chance to
    # be drawn).
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("bjt_bus")
    li1 = layout.layer(67, 20)
    paths = [s for s in top.shapes(li1).each() if s.is_path()]
    assert paths == []


def test_compose_routes_self_net_with_no_other_pad_in_the_way(tmp_path, pdk_root):
    # No regression: a self-net between a block's only two ports (nothing
    # else on the block to cross) must still route -- #433's check only
    # rejects a backbone that actually overlaps another pad, not every
    # same-block net.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "unit",
        rows=1,
        cols=1,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    output = tmp_path / "bjt_eb.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "EB",
                    "pins": [
                        {"block": "arr", "port": "Q0_E"},
                        {"block": "arr", "port": "Q0_B"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "bjt_eb", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0


def test_compose_self_net_pad_crossing_ignores_ports_on_a_different_layer(
    tmp_path, pdk_root
):
    # A self-net's backbone crossing over another port that reports a
    # *different* physical layer than routing.layer_role cannot short to it
    # on that layer -- only same-layer ports count as obstacles. With
    # `gate_contact` (#492) mos_array's gate ports (`U*_G`) draw on `metal`
    # (li1); a `metal2` (met1) route between two of them geometrically passes
    # right over the middle unit's own gate pad (same axis, elevated only by
    # the stub) but must still route, since that in-between port is on a
    # different layer than the met1 route being drawn -- it is reached only
    # by the via-drop at each endpoint.
    #
    # Before #492 this exercised the same crossing with a *poly* gate port
    # under a `metal` route. That pairing is no longer routable at all (a
    # metal backbone cannot reach bare poly -- see
    # test_compose_rejects_metal_route_to_bare_poly_gate_port), so the same
    # geometry is now exercised one level up the stack.
    m = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "m",
        rows=1,
        cols=3,
        topology="array",
        gate_contact=True,
    )
    output = tmp_path / "mos_gg.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "m", "generator_report": m}],
            "placement": {"strategy": "row", "order": ["m"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "GBUS",
                    "pins": [
                        {"block": "m", "port": "U0_G"},
                        {"block": "m", "port": "U2_G"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal2", "width_um": 0.17},
            "options": {"cell_name": "mos_gg", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0


@pytest.mark.parametrize("width_um", [0.22, 0.25, 0.3])
def test_compose_rejects_self_net_same_row_same_direction_pair(
    tmp_path, pdk_root, width_um
):
    # #453: the #439 pad-crossing check modelled each other port as a square of
    # its *reported* width_um, inflated by the route half-width. For an array
    # unit's base-tie pad that badly under-estimates the real drawn metal in the
    # pad's facing direction, so a self-net between two SAME-ROW, SAME-DIRECTION
    # (both north-facing) emitter ports sandwiching that base pad -- exactly the
    # issue's 8-unit common_centroid bjt_array reproduction (Q4_E<-Q0_E with
    # Q4_B between them) -- composed `routed: true` and DRC-clean while actually
    # shorting the array's shared base node into the emitter net. It reproduced
    # for every route width >= the pad's reported width_um (0.22um): the jog is
    # lifted only one stub width, so a wider route clears the under-sized
    # reported square yet still plows through the pad's real drawn metal. The
    # conservative same-direction check must reject it at all such widths.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        emitter_um=0.68,
        rows=2,
        cols=4,
        dummy=1,
        ratio=8,
        topology="common_centroid",
        add_collector_ring=False,
    )
    output = tmp_path / "pnp_bus.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "EBUS",
                    "pins": [
                        {"block": "arr", "port": "Q4_E"},
                        {"block": "arr", "port": "Q0_E"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": width_um},
            "options": {"cell_name": "pnp_bus", "output": str(output)},
        }
    )

    # The crossing self-net must not compose as routed -- before #453 this
    # returned routed: true (a silent short to the intervening Q4_B base pad).
    assert output.is_file()
    assert report["unrouted_nets"] == ["EBUS"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None
    # The explanatory note names the crossed intervening base pad.
    assert any(
        "EBUS" in note and "Q4_B" in note for note in report["drc_hints"]["notes"]
    )

    # routed: false means no metal path was drawn for the net -- the short the
    # issue describes never gets a chance to be drawn on the route layer.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("pnp_bus")
    li1 = layout.layer(67, 20)
    assert [s for s in top.shapes(li1).each() if s.is_path()] == []


# --------------------------------------------------------------------------- #
# Route-vs-route collision (#1057): route_two_pin()'s six routability checks
# (channel width, guard/collector-ring, self-net pad-crossing, self-net
# drawn-metal, obstacle-overlap, via-drop resolution) each compare a single
# net's own backbone against *block* geometry -- none of them compares one
# connectivity[] entry's drawn backbone against *another* entry's already-
# accepted one. Two distinct nets routed on the same routing.layer_role could
# be drawn crossing each other, both composing `routed: true`, silently
# shorting a caller-declared distinct net pair -- the one failure mode in the
# router that was silent-and-wrong rather than conservative-and-refused. This
# is checked in `compose()`'s connectivity loop, order-aware just like every
# other check here: a net is compared only against whatever routed_geometry
# already exists at the time it is processed.
# --------------------------------------------------------------------------- #


def _crossing_routes_fixture(tmp_path, pdk_root, width_um=0.17):
    """Four `resistor_strip` blocks, placed (`"explicit"`) so that a straight
    horizontal net (``NET_H``, west block to east block, no waypoints needed)
    and a caller-routed vertical net (``NET_V``, north block to south block,
    via ``waypoints_um``) are geometrically forced to cross at (16, ~15.21) --
    an inverter-chain-shaped fixture's crossing reduced to its essential
    shape: two backbones whose fixed paths intersect, nowhere near any
    block's own bbox (each pair's stub only ever moves *away* from its own
    block, so the existing route-vs-block checks stay silent -- this is
    purely a route-vs-route collision).
    """
    bw = _gen_block(tmp_path, pdk_root, "resistor_strip", "bw", num=1)
    be = _gen_block(tmp_path, pdk_root, "resistor_strip", "be", num=1)
    bn = _gen_block(tmp_path, pdk_root, "resistor_strip", "bn", num=1)
    bs = _gen_block(tmp_path, pdk_root, "resistor_strip", "bs", num=1)
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "bw", "generator_report": bw},
            {"id": "be", "generator_report": be},
            {"id": "bn", "generator_report": bn},
            {"id": "bs", "generator_report": bs},
        ],
        "placement": {
            "strategy": "explicit",
            "order": ["bw", "be", "bn", "bs"],
            "origins_um": {
                "bw": {"x": 0.0, "y": 15.0},
                "be": {"x": 30.0, "y": 15.0},
                "bn": {"x": 10.0, "y": 25.0},
                "bs": {"x": 18.0, "y": 5.0},
            },
        },
        "connectivity": [
            {
                "net": "NET_H",
                "pins": [
                    {"block": "bw", "port": "P2"},
                    {"block": "be", "port": "P1"},
                ],
            },
            {
                "net": "NET_V",
                "pins": [
                    {"block": "bn", "port": "P2"},
                    {"block": "bs", "port": "P1"},
                ],
                # Forces a vertical drop through x=16, crossing NET_H's
                # straight y=15.21 backbone -- see the module docstring
                # above: waypoints_um switches off only the *fixed-shape*
                # heuristics (channel width, degenerate same-dir jog, #461
                # stub lift), never the checks that look at the actual drawn
                # `points`, which is exactly what the route-vs-route check
                # does.
                "waypoints_um": [[16.0, 25.21], [16.0, 5.21]],
            },
        ],
        "routing": {"layer_role": "metal", "width_um": width_um},
    }


def test_compose_rejects_route_that_crosses_an_already_routed_net(tmp_path, pdk_root):
    output = tmp_path / "cross.gds"
    request = _crossing_routes_fixture(tmp_path, pdk_root)
    request["options"] = {"cell_name": "cross", "output": str(output)}
    report = compose(request)

    assert output.is_file()
    # NET_H, processed first (connectivity[] order), routes cleanly.
    assert report["nets"][0]["net"] == "NET_H"
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] is not None

    # NET_V's backbone is geometrically forced to cross NET_H's already-
    # accepted backbone -- caught here instead of composing `routed: true`
    # for both and silently drawing a short.
    assert report["nets"][1]["net"] == "NET_V"
    assert report["nets"][1]["routed"] is False
    assert report["nets"][1]["route_length_um"] is None
    assert report["unrouted_nets"] == ["NET_V"]
    assert any(
        "NET_V" in note and "crosses" in note and "NET_H" in note
        for note in report["drc_hints"]["notes"]
    )

    # routed: false means no metal path was drawn for NET_V -- the short
    # never gets a chance to be drawn on the route layer.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("cross")
    li1 = layout.layer(67, 20)
    paths = [s for s in top.shapes(li1).each() if s.is_path()]
    assert len(paths) == 1  # NET_H's path only


def test_compose_route_vs_route_collision_is_order_dependent(tmp_path, pdk_root):
    # Consistent with every other route_two_pin() check: a net is compared
    # only against whatever geometry already exists at the time it is
    # processed. Reversing connectivity[] order flips which net "wins" --
    # NET_V (now first) routes, NET_H (now second) is rejected instead.
    output = tmp_path / "cross_reversed.gds"
    request = _crossing_routes_fixture(tmp_path, pdk_root)
    request["connectivity"] = list(reversed(request["connectivity"]))
    request["options"] = {"cell_name": "cross_reversed", "output": str(output)}
    report = compose(request)

    assert report["nets"][0]["net"] == "NET_V"
    assert report["nets"][0]["routed"] is True
    assert report["nets"][1]["net"] == "NET_H"
    assert report["nets"][1]["routed"] is False
    assert report["unrouted_nets"] == ["NET_H"]
    assert any(
        "NET_H" in note and "crosses" in note and "NET_V" in note
        for note in report["drc_hints"]["notes"]
    )


def test_compose_rejects_same_block_self_net_pair_whose_via_landing_pads_cross(
    tmp_path, pdk_root
):
    # Issue #1197: two same-block self-nets (#439/#453's territory) whose
    # *bare backbones* never touch each other could still compose
    # `routed: true` for both while shorting on the composed layer -- not
    # because the backbones themselves cross, but because the via-drop
    # landing pad `_write_composed_gds` draws at one net's own endpoint
    # (`_VIA_LANDING_SIZE_UM`, independent of and wider than the route's own
    # `width_um`) lands on top of the *other* net's already-accepted
    # backbone. #1057's route-vs-route collision check compared only the
    # bare backbone points, so it never saw this -- exactly the reproduction
    # from the issue: a common-centroid 3x6 nfet `mos_array`, self-net NA
    # bussing two drains (U10_D, U11_D) and self-net NB bussing two sources
    # (U4_S, U12_S), on the very same block, whose via landing pads cross
    # each other's backbone without either backbone crossing the other.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "arr",
        w_um=1.0,
        l_um=0.15,
        rows=3,
        cols=6,
        dummy=0,
        flavor="nfet",
        topology="common_centroid",
    )
    output = tmp_path / "self_net_pair.gds"
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": "arr", "generator_report": arr}],
        "placement": {
            "strategy": "explicit",
            "order": ["arr"],
            "origins_um": {"arr": {"x": 0.0, "y": 0.0}},
        },
        "connectivity": [
            {
                "net": "NA",
                "pins": [
                    {"block": "arr", "port": "U10_D"},
                    {"block": "arr", "port": "U11_D"},
                ],
            },
            {
                "net": "NB",
                "pins": [
                    {"block": "arr", "port": "U4_S"},
                    {"block": "arr", "port": "U12_S"},
                ],
            },
        ],
        "routing": {"layer_role": "metal2", "width_um": 0.3},
        "options": {"cell_name": "self_net_pair", "output": str(output)},
    }
    report = compose(request)

    # The two nets must never both compose `routed: true` when their drawn
    # footprints (backbone + via landing pads) actually collide -- the
    # second one processed is rejected with a `legs[].reason` naming the
    # first, mirroring #439/#1057's fail-visible pattern, rather than both
    # silently drawing a short certified `routed: true`.
    assert not all(net["routed"] for net in report["nets"])
    rejected = [net for net in report["nets"] if not net["routed"]]
    assert len(rejected) == 1
    accepted_name = "NA" if rejected[0]["net"] == "NB" else "NB"
    blocker_leg = next(leg for leg in rejected[0]["legs"] if leg["reason"] is not None)
    assert "crosses already-routed net" in blocker_leg["reason"]
    assert accepted_name in blocker_leg["reason"]
    assert report["unrouted_nets"] == [rejected[0]["net"]]

    # Whatever gen-compose reported, the composed layout itself must never
    # show the two declared net names merged onto one electrical node.
    result = extract.run_extract(str(output), "sky130", top="self_net_pair")
    assert result["merged_net_labels"] == []
    assert not any(
        "NA" in net["name"] and "NB" in net["name"] for net in result["nets"]
    )


def test_compose_allows_disjoint_routes_on_the_same_layer(tmp_path, pdk_root):
    # No regression: two nets on the same routing.layer_role that do *not*
    # cross must both still route -- the collision check must not fire on
    # backbones that merely coexist on the same layer. Two independent
    # west-to-east straight-line pairs (same shape as NET_H above), stacked
    # at different y so neither backbone ever comes near the other.
    width_um = 0.17
    bw1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "bw1", num=1)
    be1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "be1", num=1)
    bw2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "bw2", num=1)
    be2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "be2", num=1)
    output = tmp_path / "nocross.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "bw1", "generator_report": bw1},
                {"id": "be1", "generator_report": be1},
                {"id": "bw2", "generator_report": bw2},
                {"id": "be2", "generator_report": be2},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["bw1", "be1", "bw2", "be2"],
                "origins_um": {
                    "bw1": {"x": 0.0, "y": 15.0},
                    "be1": {"x": 30.0, "y": 15.0},
                    # NET_2 is a second straight west-to-east line, well
                    # clear (10um) of NET_1's y=15.21 line.
                    "bw2": {"x": 0.0, "y": 25.0},
                    "be2": {"x": 30.0, "y": 25.0},
                },
            },
            "connectivity": [
                {
                    "net": "NET_1",
                    "pins": [
                        {"block": "bw1", "port": "P2"},
                        {"block": "be1", "port": "P1"},
                    ],
                },
                {
                    "net": "NET_2",
                    "pins": [
                        {"block": "bw2", "port": "P2"},
                        {"block": "be2", "port": "P1"},
                    ],
                },
            ],
            "routing": {"layer_role": "metal", "width_um": width_um},
            "options": {"cell_name": "nocross", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][1]["routed"] is True


def test_compose_route_dbu_probe_wraps_bad_gds_path_in_gen_compose_error(
    tmp_path, pdk_root
):
    # Regression: `_route_dbu()` (only invoked when connectivity[] is
    # non-empty) reads `blocks[order[0]]["gds_path"]` via a bare
    # `kdb.Layout().read()`, same as `read_block_layer_geometry()` and
    # `_write_composed_gds()` -- both of which wrap that call in
    # try/except and re-raise `GenComposeError` because klayout raises a
    # bare `RuntimeError` for a missing/corrupt GDS file. Before that
    # wrapping was added to `_route_dbu()` too, a bad `gds_path` on the
    # first-placed block escaped compose() as an unhandled `RuntimeError`
    # instead of the documented JSON error envelope.
    b1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "b1")
    b2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "b2")
    b1["gds_path"] = str(tmp_path / "does-not-exist.gds")
    with pytest.raises(GenComposeError, match="does-not-exist.gds"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": b1},
                    {"id": "b2", "generator_report": b2},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["b1", "b2"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "b1", "port": "P2"},
                            {"block": "b2", "port": "P1"},
                        ],
                    }
                ],
                "routing": {"layer_role": "metal", "width_um": 0.17},
                "options": {
                    "cell_name": "bad_gds_path",
                    "output": str(tmp_path / "bad_gds_path.gds"),
                },
            }
        )


# --------------------------------------------------------------------------- #
# Via-drop routing (#454, re-raising #433's Ask options 1/2): a `"metal2"`
# `routing.layer_role` runs the backbone on sky130's met1 and drops to each
# target pin's own li1 pad only via the connecting mcon via
# (`_resolve_via_drop_layer`) -- the exact same-block bus #433 could only
# reject (`unrouted_nets[]`) is now routable.
# --------------------------------------------------------------------------- #


def test_resolve_via_drop_layer_same_layer_needs_no_drop():
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (67, 20), (67, 20))
    assert via_layer is None
    assert error is None


def test_resolve_via_drop_layer_unrelated_role_needs_no_drop():
    # A tap port (65/44) is not a member of the deck's metals stack at all,
    # and is not the deck's poly layer either -- via-drop only ever applies
    # between two declared routing-metal levels, so this keeps the pre-#454
    # "draw directly on route_layer" behavior rather than being treated as
    # "needs a drop but none found".
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (67, 20), (65, 44))
    assert via_layer is None
    assert error is None


def test_resolve_via_drop_layer_bare_poly_gate_port_is_rejected():
    # #492: a metal backbone ending on the deck's bare `poly` layer (66/20 on
    # sky130 -- a gate drawn without params.gate_contact) has no via that
    # reaches it. Before #492 this fell into the "unrelated role, nothing to
    # do" branch and the net was drawn anyway: `routed: true`, no note, and a
    # metal stub sitting over the gate with no contact joining the two.
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (67, 20), deck.poly)
    assert via_layer is None
    assert error is not None
    assert "bare-poly gate" in error
    assert "gate_contact" in error


def test_resolve_via_drop_layer_poly_route_layer_still_draws_directly():
    # The rejection above is about the *pin's* layer, not the backbone's: a
    # route whose own layer_role resolves outside the metals stack (e.g.
    # "poly"/"tap") has no stack to walk and keeps drawing directly.
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, deck.poly, (67, 20))
    assert via_layer is None
    assert error is None


def test_resolve_via_drop_layer_adjacent_metals_resolves_the_via():
    # sky130's metals=((67,20),(68,20)), vias=((67,44),) -- li1 (metals[0])
    # to met1 (metals[1]) resolves to mcon.
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (68, 20), (67, 20))
    assert via_layer == (67, 44)
    assert error is None


def test_resolve_via_drop_layer_non_adjacent_metals_is_unresolvable():
    # sky130's metals stack is now three levels deep (li1/met1/met2, issue
    # #508's third connectivity level) -- a route on met2 (metals[2],
    # "metal3") to a pin still on li1 (metals[0], the base "metal" role) is
    # two via hops apart, exercising the >1-hop rejection path against the
    # real deck (no synthetic three-level deck needed anymore, unlike before
    # #508 extended the real one).
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (69, 20), (67, 20))
    assert via_layer is None
    assert error is not None
    assert "single-hop" in error


def test_resolve_via_drop_layer_metal3_to_metal2_resolves_the_via():
    # A route on met2 (metals[2], "metal3") to a pin on met1 (metals[1],
    # "metal2") is exactly one via hop apart -- resolves to the met1<->met2
    # via (issue #508).
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (69, 20), (68, 20))
    assert via_layer == (68, 44)
    assert error is None


def test_resolve_route_layer_metal3_and_via2_roles():
    # `routing.layer_role`/the connecting via role resolve through the same
    # `_PDK_ROLE_LAYERS` table `_resolve_via_drop_layer` above reads off the
    # deck directly -- confirming the router-facing role names (#508) match
    # the deck's own met2/via.drawing layers.
    assert gen_compose._resolve_route_layer("sky130A", "metal3") == (69, 20)
    assert gen_compose._resolve_route_layer("sky130A", "via2") == (68, 44)


def test_resolve_via_drop_layer_metal3_to_metal2_resolves_the_via_gf180mcu():
    # gf180mcu equivalent of the sky130 case above (issue #1058): a route on
    # Metal3 (metals[2], "metal3") to a pin on Metal2 (metals[1], "metal2")
    # is exactly one via hop apart -- resolves to the Metal2<->Metal3 via
    # (Via2, 38/0).
    deck = get_extraction_deck("gf180mcu")
    via_layer, error = _resolve_via_drop_layer(deck, (42, 0), (36, 0))
    assert via_layer == (38, 0)
    assert error is None


def test_resolve_via_drop_layer_non_adjacent_metals_is_unresolvable_gf180mcu():
    # gf180mcu equivalent of the sky130 case above (issue #1058): a route on
    # Metal3 (metals[2], "metal3") to a pin still on Metal1 (metals[0], the
    # base "metal" role) is two via hops apart, exercising the >1-hop
    # rejection path against the real gf180mcu deck.
    deck = get_extraction_deck("gf180mcu")
    via_layer, error = _resolve_via_drop_layer(deck, (42, 0), (34, 0))
    assert via_layer is None
    assert error is not None
    assert "single-hop" in error


def test_resolve_route_layer_metal3_and_via2_roles_gf180mcu():
    # `routing.layer_role`/the connecting via role resolve through the same
    # `_PDK_ROLE_LAYERS` table `_resolve_via_drop_layer` above reads off the
    # deck directly -- confirming the router-facing role names (issue #1058)
    # match gf180mcu's own Metal3/Via2 layers.
    assert gen_compose._resolve_route_layer("gf180mcuA", "metal3") == (42, 0)
    assert gen_compose._resolve_route_layer("gf180mcuA", "via2") == (38, 0)


def test_compose_via_drop_routes_self_net_that_pure_metal_would_reject(
    tmp_path, pdk_root
):
    # The exact #433 reproduction (an 8-unit bjt_array, bussing three
    # emitters Q0_E/Q1_E/Q2_E into one node via two 2-pin self-nets, each
    # backbone jogging directly over the base pad sitting between the
    # emitters it connects) -- but routed on `"metal2"` instead of `"metal"`.
    # Where `test_compose_rejects_self_net_that_crosses_another_pad_on_same_block`
    # asserts both nets land in `unrouted_nets[]`, this asserts both now
    # route: the backbone runs on met1, never touching the li1 base pad it
    # geometrically crosses over, and drops to each target emitter's own li1
    # pad only via an mcon via at that pad's own position.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        rows=1,
        cols=8,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    output = tmp_path / "bjt_bus_via_drop.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "EBUS1",
                    "pins": [
                        {"block": "arr", "port": "Q0_E"},
                        {"block": "arr", "port": "Q1_E"},
                    ],
                },
                {
                    "net": "EBUS2",
                    "pins": [
                        {"block": "arr", "port": "Q1_E"},
                        {"block": "arr", "port": "Q2_E"},
                    ],
                },
            ],
            "routing": {"layer_role": "metal2", "width_um": 0.17},
            "options": {"cell_name": "bjt_bus_via_drop", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0
    assert report["nets"][1]["routed"] is True
    assert report["nets"][1]["route_length_um"] > 0

    # The backbone is drawn on met1 (68/20), not li1 -- and a via (mcon,
    # 67/44) plus li1 landing pad were dropped at each of the four pin
    # endpoints (Q0_E, Q1_E used twice as the shared middle pin, Q2_E).
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("bjt_bus_via_drop")
    li1 = layout.layer(67, 20)
    met1 = layout.layer(68, 20)
    mcon = layout.layer(67, 44)
    assert [s for s in top.shapes(met1).each() if s.is_path()]
    assert [s for s in top.shapes(li1).each() if s.is_path()] == []
    assert list(top.shapes(mcon).each())  # at least one via drawn

    # DRC-clean (acceptance criterion): the via-drop's own drawn geometry
    # (via + landing pads on both met1 and li1) must not violate any curated
    # sky130 rule (li1/met1 width/space, met1.enclosing.mcon, mcon.space).
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    # Extraction merges only the three targeted emitters into one node --
    # every other pad (the other five emitters, every base tie) stays its
    # own distinct node.
    result = extract.run_extract(str(output), "sky130", top="bjt_bus_via_drop")
    bjt_devices = [d for d in result["devices"] if d["class"] == "pnp"]
    assert len(bjt_devices) == 8
    emitter_nets = {d["name"]: d["nets"]["e"] for d in bjt_devices}
    bussed = {name: net for name, net in emitter_nets.items() if net is not None}
    # Exactly 3 devices' emitters share one common net name...
    from collections import Counter

    counts = Counter(bussed.values())
    assert 3 in counts.values(), emitter_nets
    bussed_net = next(net for net, n in counts.items() if n == 3)
    bussed_devices = {name for name, net in emitter_nets.items() if net == bussed_net}
    assert len(bussed_devices) == 3
    # ...and no base ('b') terminal shares that same net (the bus stayed off
    # the base pad it geometrically crossed over on met1).
    base_nets = {d["nets"]["b"] for d in bjt_devices}
    assert bussed_net not in base_nets


# --------------------------------------------------------------------------- #
# Cross-block bus routing (routing.cross_block_layer_role, #1168): unlike the
# via-drop tests above -- which route the *whole composition* on "metal2" --
# a single `routing.layer_role: "metal"` composition can now name a *second*
# layer_role that only a same-block self-net leg falls back to when it would
# otherwise short across another of the block's own same-layer pads, leaving
# every other net in the same request on the primary layer.
# --------------------------------------------------------------------------- #


def test_resolve_cross_block_route_layer_resolves_adjacent_metals():
    # sky130's "metal" (li1, 67/20) and "metal2" (met1, 68/20) are exactly one
    # via hop apart (mcon, 67/44) -- the same pair the via-drop tests above
    # use, just resolved from the opposite direction.
    cross_layer, via_layer = gen_compose._resolve_cross_block_route_layer(
        "sky130A", "metal", "metal2"
    )
    assert cross_layer == (68, 20)
    assert via_layer == (67, 44)


def test_resolve_cross_block_route_layer_rejects_identical_roles():
    with pytest.raises(GenComposeError, match="same layer"):
        gen_compose._resolve_cross_block_route_layer("sky130A", "metal", "metal")


def test_resolve_cross_block_route_layer_rejects_non_adjacent_metals():
    # "metal" (li1, metals[0]) and "metal3" (met2, metals[2]) are two via
    # hops apart -- not resolvable by a single hop, mirroring
    # _resolve_via_drop_layer's own non-adjacent rejection.
    with pytest.raises(GenComposeError, match="single via"):
        gen_compose._resolve_cross_block_route_layer("sky130A", "metal", "metal3")


def test_resolve_cross_block_route_layer_gf180mcu():
    cross_layer, via_layer = gen_compose._resolve_cross_block_route_layer(
        "gf180mcuA", "metal2", "metal3"
    )
    assert cross_layer == (42, 0)
    assert via_layer == (38, 0)


def test_compose_rejects_unknown_cross_block_layer_role(tmp_path, pdk_root):
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        rows=1,
        cols=8,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    with pytest.raises(GenComposeError, match="cross_block_layer_role"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "arr", "generator_report": arr}],
                "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
                "connectivity": [
                    {
                        "net": "EBUS1",
                        "pins": [
                            {"block": "arr", "port": "Q0_E"},
                            {"block": "arr", "port": "Q1_E"},
                        ],
                    }
                ],
                "routing": {
                    "layer_role": "metal",
                    "width_um": 0.17,
                    "cross_block_layer_role": "not-a-real-role",
                },
                "options": {
                    "cell_name": "bad_cross_role",
                    "output": str(tmp_path / "bad_cross_role.gds"),
                },
            }
        )


def test_compose_cross_block_layer_role_routes_the_exact_433_reproduction(
    tmp_path, pdk_root
):
    # Byte-identical reproduction and request to
    # test_compose_rejects_self_net_that_crosses_another_pad_on_same_block
    # above, except `routing.layer_role` stays "metal" (unlike the via-drop
    # test, which moves the *entire* composition to "metal2") and instead
    # names "metal2" as `routing.cross_block_layer_role` -- both EBUS1/EBUS2
    # now route, falling back to met1 only for these two crossing legs.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        rows=1,
        cols=8,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    output = tmp_path / "bjt_bus_cross_block.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "EBUS1",
                    "pins": [
                        {"block": "arr", "port": "Q0_E"},
                        {"block": "arr", "port": "Q1_E"},
                    ],
                },
                {
                    "net": "EBUS2",
                    "pins": [
                        {"block": "arr", "port": "Q1_E"},
                        {"block": "arr", "port": "Q2_E"},
                    ],
                },
            ],
            "routing": {
                "layer_role": "metal",
                "width_um": 0.17,
                "cross_block_layer_role": "metal2",
            },
            "options": {"cell_name": "bjt_bus_cross_block", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0
    assert report["nets"][1]["routed"] is True
    assert report["nets"][1]["route_length_um"] > 0

    # Both legs' backbones drew on met1 (68/20), with an mcon via (67/44) at
    # each endpoint back down to the emitters' own li1 pads -- li1 itself
    # carries no *new path* (only the pre-existing block geometry plus the
    # via-drop's own landing-pad boxes, never a kdb.Path).
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("bjt_bus_cross_block")
    met1 = layout.layer(68, 20)
    mcon = layout.layer(67, 44)
    assert [s for s in top.shapes(met1).each() if s.is_path()]
    assert list(top.shapes(mcon).each())

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    result = extract.run_extract(str(output), "sky130", top="bjt_bus_cross_block")
    bjt_devices = [d for d in result["devices"] if d["class"] == "pnp"]
    assert len(bjt_devices) == 8
    emitter_nets = {d["name"]: d["nets"]["e"] for d in bjt_devices}
    bussed = {name: net for name, net in emitter_nets.items() if net is not None}
    from collections import Counter

    counts = Counter(bussed.values())
    assert 3 in counts.values(), emitter_nets
    bussed_net = next(net for net, n in counts.items() if n == 3)
    bussed_devices = {name for name, net in emitter_nets.items() if net == bussed_net}
    assert len(bussed_devices) == 3
    base_nets = {d["nets"]["b"] for d in bjt_devices}
    assert bussed_net not in base_nets


def test_compose_cross_block_layer_role_leaves_non_crossing_nets_on_primary_layer(
    tmp_path, pdk_root
):
    # #1168's own point: `routing.cross_block_layer_role` is a *per-leg*
    # fallback, not a whole-composition layer switch like `routing.
    # layer_role: "metal2"` (the via-drop tests above) is. One composition
    # with three blocks -- a bjt_array whose own self-net bus must cross its
    # middle unit's base pad, plus two independent mos_array blocks joined by
    # an ordinary block-to-block net -- routes the crossing legs on met1 and
    # leaves the untouched block-to-block net on li1, exactly as it would
    # with no `cross_block_layer_role` configured at all.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        rows=1,
        cols=8,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    m1 = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "m1",
        rows=1,
        cols=1,
        dummy=0,
        gate_contact=True,
    )
    m2 = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "m2",
        rows=1,
        cols=1,
        dummy=0,
        gate_contact=True,
    )
    output = tmp_path / "mixed_layer_bus.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "arr", "generator_report": arr},
                {"id": "b1", "generator_report": m1},
                {"id": "b2", "generator_report": m2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["arr", "b1", "b2"],
                "spacing_um": 2.0,
            },
            "connectivity": [
                {
                    "net": "EBUS1",
                    "pins": [
                        {"block": "arr", "port": "Q0_E"},
                        {"block": "arr", "port": "Q1_E"},
                    ],
                },
                {
                    "net": "GBIAS",
                    "pins": [
                        {"block": "b1", "port": "U0_G"},
                        {"block": "b2", "port": "U0_G"},
                    ],
                },
            ],
            "routing": {
                "layer_role": "metal",
                "width_um": 0.17,
                "cross_block_layer_role": "metal2",
            },
            "options": {"cell_name": "mixed_layer_bus", "output": str(output)},
        }
    )

    assert output.is_file()
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["net"] == "EBUS1"
    assert report["nets"][0]["routed"] is True
    assert report["nets"][1]["net"] == "GBIAS"
    assert report["nets"][1]["routed"] is True

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("mixed_layer_bus")
    li1 = layout.layer(67, 20)
    met1 = layout.layer(68, 20)
    li1_paths = [s for s in top.shapes(li1).each() if s.is_path()]
    met1_paths = [s for s in top.shapes(met1).each() if s.is_path()]
    # EBUS1's leg fell back to met1; GBIAS's own backbone still runs on li1 --
    # both drawing layers carry exactly one routed kdb.Path each.
    assert len(li1_paths) == 1
    assert len(met1_paths) == 1

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


# --------------------------------------------------------------------------- #
# Gate-port routing (#492): before this, a `connectivity[]` net naming a
# bare-poly gate port was drawn as a metal stub *over* the gate with no
# contact joining the two -- `routed: true`, no note, and an open net only a
# later `klt drc`/`klt extract`/`klt lvs` run would surface. The router now
# rejects that pairing outright, and `klt gen`'s `params.gate_contact`
# finishes the gate stack so the same net routes end to end.
# --------------------------------------------------------------------------- #


def test_compose_rejects_metal_route_to_bare_poly_gate_port(tmp_path, pdk_root):
    # A metal backbone cannot reach a bare-poly gate: no via in the deck's
    # metals stack lands on poly. Reported unroutable with a reason naming
    # both the port's actual layer and the fix -- never drawn as a silently
    # uncontacted stub.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "m1", rows=1, cols=1)
    m2 = _gen_block(tmp_path, pdk_root, "mos_array", "m2", rows=1, cols=1)
    output = tmp_path / "bare_gate.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": m1},
                {"id": "b2", "generator_report": m2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 2.0},
            "connectivity": [
                {
                    "net": "GBIAS",
                    "pins": [
                        {"block": "b1", "port": "U0_G"},
                        {"block": "b2", "port": "U0_G"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "bare_gate_0", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == ["GBIAS"]
    assert report["nets"][0]["routed"] is False
    reason = next(
        note for note in report["drc_hints"]["notes"] if note.startswith("net 'GBIAS'")
    )
    assert "(66, 20)" in reason  # the port's actual (poly) layer
    assert "bare-poly gate" in reason
    assert "gate_contact" in reason


def test_compose_routes_gate_contact_port_end_to_end(tmp_path, pdk_root):
    # The #492 acceptance case: with `params.gate_contact` the gate reports on
    # the metal role, so a `connectivity[]` net wires two devices' gates
    # together with no hand-drawn licon/li1 patchwork -- and the result is one
    # continuous conductor (not two stubs), DRC-clean, and extracts with both
    # gates on the named net.
    # `dummy=0`: a dummy column's own (also contacted) gate pad sits at exactly
    # the height the inter-gate backbone runs at, and the router's obstacle
    # checks only model a block's *reported* ports -- routing around a block's
    # unreported dummy geometry is a separate, pre-existing limitation.
    m1 = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "m1",
        rows=1,
        cols=1,
        dummy=0,
        gate_contact=True,
    )
    m2 = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "m2",
        rows=1,
        cols=1,
        dummy=0,
        gate_contact=True,
    )
    g1 = next(p for p in m1["ports"] if p["name"] == "U0_G")
    g2 = next(p for p in m2["ports"] if p["name"] == "U0_G")
    assert g1["layer"] == {"layer": 67, "datatype": 20, "name": None}

    output = tmp_path / "gate_routed.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": m1},
                {"id": "b2", "generator_report": m2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 2.0},
            "connectivity": [
                {
                    "net": "GBIAS",
                    "pins": [
                        {"block": "b1", "port": "U0_G"},
                        {"block": "b2", "port": "U0_G"},
                    ],
                }
            ],
            # A pad-wide trace. Both gate ports face north, so the backbone
            # jogs up past each pad's top edge; a trace *narrower* than the
            # pad leaves a sub-li1.space.1 slit between the pad's top edge
            # and the backbone's underside beside the stub. That is a generic
            # router property for any north/south-facing port whose pad is
            # wider than routing.width_um (a guard ring's TAP_N, a bjt
            # COLL_N), not something this issue introduced -- keep the trace
            # as wide as the pad so this test measures the gate contact.
            "routing": {"layer_role": "metal", "width_um": 0.42},
            "options": {"cell_name": "gate_routed_0", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True

    # Both gate pads and the backbone between them are one merged li1
    # polygon -- a real conductor, not the two disconnected stubs the
    # pre-#492 silent path produced.
    b1_off = next(b for b in report["blocks"] if b["id"] == "b1")["offset_um"]
    b2_off = next(b for b in report["blocks"] if b["id"] == "b2")["offset_um"]
    p1 = (g1["x_um"] + b1_off["x"], g1["y_um"] + b1_off["y"])
    p2 = (g2["x_um"] + b2_off["x"], g2["y_um"] + b2_off["y"])
    assert _shares_merged_polygon(output, "gate_routed_0", 67, 20, p1, p2)
    # ...and each gate's own licon is there, joining that metal to the poly.
    assert _shares_merged_polygon(output, "gate_routed_0", 66, 44, p1, p1)
    assert _shares_merged_polygon(output, "gate_routed_0", 66, 44, p2, p2)

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    # Extraction sees one named gate net shared by both devices -- the closed
    # loop the issue asks for (no hand-drawn contact anywhere in this test).
    result = extract.run_extract(str(output), "sky130", top="gate_routed_0")
    assert "GBIAS" in {net["name"] for net in result["nets"] if net["pin"]}
    gate_nets = [d["nets"]["g"] for d in result["devices"] if d["class"] == "nfet"]
    assert gate_nets == ["GBIAS", "GBIAS"], result["devices"]


# --------------------------------------------------------------------------- #
# Self-net drawn-metal crossing (#453/#469): the #433/#439 pad-crossing check
# (and #467's same-row/same-direction fallback) model each other port as a
# square built from its *reported* `width_um` -- a port's contact/access
# size, not the extent of the pad metal actually drawn around it. Both checks
# only fire for a degenerate single-jog backbone (same row/column, same
# facing direction), so they miss a same-facing pair on *different* rows, or
# a route wide enough to reach an adjacent row's pad. `route_two_pin` must
# additionally compare the route's *drawn* metal against the block's own
# *drawn* shapes on the route layer (`read_block_layer_geometry`).
# --------------------------------------------------------------------------- #


def _bjt_array_8(tmp_path, pdk_root):
    return _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "pnp_test",
        emitter_um=0.68,
        rows=2,
        cols=4,
        dummy=1,
        ratio=8,
        topology="common_centroid",
        add_collector_ring=False,
    )


def _bjt_self_net_request(pdk_root, arr, output, pin_a, pin_b, width_um):
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": "pnp", "generator_report": arr}],
        "placement": {"strategy": "row", "order": ["pnp"], "spacing_um": 1.0},
        "connectivity": [
            {
                "net": "N",
                "pins": [
                    {"block": "pnp", "port": pin_a},
                    {"block": "pnp", "port": pin_b},
                ],
            }
        ],
        "routing": {"layer_role": "metal", "width_um": width_um},
        "options": {"cell_name": "pnp_bus", "output": str(output)},
    }


@pytest.mark.parametrize("width_um", [0.17, 0.22])
def test_compose_rejects_self_net_between_same_facing_ports_on_different_rows(
    tmp_path, pdk_root, width_um
):
    # Q4_E (row 0) and Q3_E (row 1) both face north but sit on different rows
    # and columns, so neither #439's inflated-pad-footprint check nor #467's
    # same-row/same-direction fallback fires (both require the two pins to
    # share their facing-axis coordinate). The route's drawn metal still
    # crosses another unit's real drawn emitter/base pads on its way across
    # the array -- exactly the "different rows" gap the issue's table
    # measures, at both route widths below the pad's own reported width_um
    # (0.22) and at it.
    arr = _bjt_array_8(tmp_path, pdk_root)
    output = tmp_path / "pnp_bus.gds"
    report = compose(
        _bjt_self_net_request(pdk_root, arr, output, "Q4_E", "Q3_E", width_um)
    )

    assert report["unrouted_nets"] == ["N"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None
    assert any("N" in note for note in report["drc_hints"]["notes"])

    # routed: false means no metal path was drawn for the net.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("pnp_bus")
    li1 = layout.layer(67, 20)
    assert [s for s in top.shapes(li1).each() if s.is_path()] == []


def test_compose_rejects_self_net_between_adjacent_ports_with_a_wide_route(
    tmp_path, pdk_root
):
    # Q4_E and Q4_B are directly adjacent, same row, same facing direction --
    # with nothing else reported as a port between them, so #439/#467's
    # reported-geometry pad models see no obstacle at all. A route wide
    # enough (0.5um, more than double any port's reported width_um) still
    # draws metal that lands on another unit's real drawn pad -- only the
    # block's actual drawn shapes (not its reported ports[]) show that.
    arr = _bjt_array_8(tmp_path, pdk_root)
    output = tmp_path / "pnp_bus.gds"
    report = compose(_bjt_self_net_request(pdk_root, arr, output, "Q4_E", "Q4_B", 0.50))

    assert report["unrouted_nets"] == ["N"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None


def test_route_two_pin_same_row_pair_needs_drawn_geometry_to_be_caught(
    tmp_path, pdk_root
):
    # Direct route_two_pin() regression: without block_geometry (the
    # pre-#469 information set), the different-row Q4_E-Q3_E short from the
    # test above is *not* caught by checks 1-3 alone -- it is only when
    # route_two_pin() is given the block's actual drawn shapes on the route
    # layer that check 4 catches it.
    arr = _bjt_array_8(tmp_path, pdk_root)
    blocks = gen_compose._parse_blocks([{"id": "pnp", "generator_report": arr}])
    offsets = {"pnp": {"x": 0.0, "y": 0.0}}
    bboxes = {"pnp": blocks["pnp"]["bbox_um"]}
    pin_a = {"block": "pnp", "port": "Q4_E"}
    pin_b = {"block": "pnp", "port": "Q3_E"}
    route_layer = gen_compose._resolve_route_layer("sky130A", "metal")

    without = gen_compose.route_two_pin(
        pin_a, pin_b, blocks, offsets, bboxes, 0.17, route_layer
    )
    assert without["routed"] is True  # pre-#469 information set: silent short

    geometry = {
        "pnp": gen_compose.read_block_layer_geometry(
            "pnp", blocks["pnp"], offsets["pnp"], route_layer
        )
    }
    with_geometry = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.17,
        route_layer,
        block_geometry=geometry,
    )
    assert with_geometry["routed"] is False
    assert "drawn" in with_geometry["reason"]


def test_read_block_layer_geometry_returns_none_for_an_undrawn_layer(
    tmp_path, pdk_root
):
    # A block that draws nothing on the route layer contributes no obstacles
    # (and must not crash the check) -- e.g. a bjt_array has no `poly` at all.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "unit",
        rows=1,
        cols=1,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    blocks = gen_compose._parse_blocks([{"id": "arr", "generator_report": arr}])
    poly_layer = gen_compose._resolve_route_layer("sky130A", "poly")
    assert (
        gen_compose.read_block_layer_geometry(
            "arr", blocks["arr"], {"x": 0.0, "y": 0.0}, poly_layer
        )
        is None
    )


def test_compose_routes_same_direction_self_net_when_nothing_is_drawn_between(
    tmp_path, pdk_root
):
    # No false positive: two same-direction (both north-facing) ports on a
    # 1x1 array -- nothing else of the block's own drawn geometry sits
    # between them -- still route at a route width wider than either pad's
    # reported width_um. Mirrors the issue's Q0_E-Q0_B "no"-truth row.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "unit",
        rows=1,
        cols=1,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    output = tmp_path / "bjt_eb_wide.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "EB",
                    "pins": [
                        {"block": "arr", "port": "Q0_E"},
                        {"block": "arr", "port": "Q0_B"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.3},
            "options": {"cell_name": "bjt_eb_wide", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0


# --------------------------------------------------------------------------- #
# CLI: `klt gen-compose`
# --------------------------------------------------------------------------- #


def test_cli_gen_compose_json(tmp_path, pdk_root, capsys):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    request_path = tmp_path / "request.json"
    output = tmp_path / "cli_composed.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r1", "generator_report": r1}],
                "placement": {"strategy": "row", "order": ["r1"], "spacing_um": 1.0},
                "options": {"cell_name": "cli_composed", "output": str(output)},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["cell_name"] == "cli_composed"
    assert output.is_file()


def test_cli_gen_compose_generator_report_resolves_against_request_dir(
    tmp_path, pdk_root, capsys, monkeypatch
):
    # #328: blocks[].generator_report given as a path relative to the request
    # file's own directory (not the invoking cwd) must still resolve when
    # `klt gen-compose` is invoked from an unrelated cwd.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    request_dir = tmp_path / "some" / "dir"
    request_dir.mkdir(parents=True)
    (request_dir / "r1.json").write_text(json.dumps(r1))

    request_path = request_dir / "request.json"
    output = tmp_path / "cli_request_dir_relative.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r1", "generator_report": "r1.json"}],
                "placement": {"strategy": "row", "order": ["r1"], "spacing_um": 1.0},
                "options": {
                    "cell_name": "cli_request_dir_relative",
                    "output": str(output),
                },
            }
        )
    )

    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cell_name"] == "cli_request_dir_relative"
    assert output.is_file()


def test_cli_gen_compose_generator_report_relative_to_cwd_fails(
    tmp_path, pdk_root, capsys, monkeypatch
):
    # Regression guard for the bug this issue fixes: a generator_report path
    # that is only valid relative to the invoking cwd (not the request
    # file's own directory) must now fail -- confirming the CLI genuinely
    # switched to request-dir-relative resolution rather than accidentally
    # keeping cwd-relative as a fallback.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    request_dir = tmp_path / "some" / "dir"
    request_dir.mkdir(parents=True)

    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    (unrelated_cwd / "r1.json").write_text(json.dumps(r1))

    request_path = request_dir / "request.json"
    output = tmp_path / "cli_cwd_relative_should_fail.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r1", "generator_report": "r1.json"}],
                "placement": {"strategy": "row", "order": ["r1"], "spacing_um": 1.0},
                "options": {
                    "cell_name": "cli_cwd_relative_should_fail",
                    "output": str(output),
                },
            }
        )
    )

    monkeypatch.chdir(unrelated_cwd)

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert "not found" in error["error"]["message"]
    assert not output.exists()


def test_cli_gen_compose_explicit_placement_json(tmp_path, pdk_root, capsys):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    request_path = tmp_path / "request.json"
    output = tmp_path / "cli_explicit.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "r1", "generator_report": r1},
                    {"id": "r2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "explicit",
                    "order": ["r1", "r2"],
                    "origins_um": {
                        "r1": {"x": 0.0, "y": 0.0},
                        "r2": {"x": 10.0, "y": 5.0},
                    },
                },
                "options": {"cell_name": "cli_explicit", "output": str(output)},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["cell_name"] == "cli_explicit"
    r2_block = next(b for b in data["blocks"] if b["id"] == "r2")
    assert r2_block["offset_um"] == {"x": 10.0, "y": 5.0}
    assert output.is_file()


def test_cli_gen_compose_array_placement_json(tmp_path, pdk_root, capsys):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    request_path = tmp_path / "request.json"
    output = tmp_path / "cli_array.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "cell", "generator_report": r1}],
                "placement": {
                    "strategy": "array",
                    "order": ["cell"],
                    "rows": 4,
                    "cols": 2,
                    "row_pitch_um": 5.0,
                    "col_pitch_um": 20.0,
                    "origin_um": {"x": 0.0, "y": 0.0},
                },
                "options": {"cell_name": "cli_array", "output": str(output)},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["cell_name"] == "cli_array"
    cell_block = next(b for b in data["blocks"] if b["id"] == "cell")
    assert cell_block["offset_um"] == {"x": 0.0, "y": 0.0}
    assert output.is_file()

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("cli_array")
    instances = list(top.each_inst())
    assert len(instances) == 1  # exactly one hierarchical array instance
    # See test_compose_array_places_2x3_grid_as_single_hierarchical_instance's
    # comment: assert on tile count, not a specific na<->cols/nb<->rows
    # binding (KLayout's GDS AREF read/write path can swap the two).
    assert instances[0].cell_inst.na * instances[0].cell_inst.nb == 4 * 2


def test_cli_gen_compose_text(tmp_path, pdk_root, capsys):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    request_path = tmp_path / "request.json"
    output = tmp_path / "cli_composed.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r1", "generator_report": r1}],
                "placement": {"strategy": "row", "order": ["r1"], "spacing_um": 1.0},
                "options": {"cell_name": "cli_composed", "output": str(output)},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "cell_name: cli_composed" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_gen_compose_connectivity_error_exit_1(tmp_path, pdk_root, capsys):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "r1", "generator_report": r1}],
                "placement": {"strategy": "row", "order": ["r1"], "spacing_um": 1.0},
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "r1", "port": "NOPE"},
                            {"block": "r1", "port": "P1"},
                        ],
                    }
                ],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["command"] == "gen-compose"


def test_cli_gen_compose_partial_success_exit_3(tmp_path, pdk_root, capsys):
    # A net the router cannot connect is left unrouted -> partial success
    # (exit 3) with the full success payload still on stdout. Since #1073 the
    # >2-pin shape below is *routed* when its legs are routable; here b1.P1 is
    # reachable neither across b1's own body (a self-net short) nor around it
    # (plowing through b1's interior), so the net is still unrouted -- for a
    # per-leg geometric reason rather than for having more than two pins.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    request_path = tmp_path / "request.json"
    output = tmp_path / "partial.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": r1},
                    {"id": "b2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["b1", "b2"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "BUS",
                        "pins": [
                            {"block": "b1", "port": "P1"},
                            {"block": "b1", "port": "P2"},
                            {"block": "b2", "port": "P1"},
                        ],
                    }
                ],
                "routing": {"layer_role": "metal", "width_um": 0.17},
                "options": {"cell_name": "partial_0", "output": str(output)},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 3
    data = json.loads(capsys.readouterr().out)
    assert data["unrouted_nets"] == ["BUS"]
    assert output.is_file()


def test_cli_gen_compose_declare_only_partial_success_exit_3(
    tmp_path, pdk_root, capsys
):
    # #1188: connectivity[] with no `routing` key at all still succeeds (not
    # exit 1), and reports through the same "partial success" exit code (3)
    # a geometrically-unroutable net does -- never exit 0 (which would
    # misreport "fully routed").
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    request_path = tmp_path / "request.json"
    output = tmp_path / "declare_cli.gds"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": r1},
                    {"id": "b2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["b1", "b2"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "b1", "port": "P2"},
                            {"block": "b2", "port": "P1"},
                        ],
                    }
                ],
                "options": {"cell_name": "declare_cli_0", "output": str(output)},
            }
        )
    )

    exit_code = main(["gen-compose", str(request_path), "--format", "json"])
    assert exit_code == 3
    data = json.loads(capsys.readouterr().out)
    assert data["unrouted_nets"] == ["N1"]
    assert data["nets"][0]["status"] == "unrouted"
    assert data["nets"][0]["legs"][0]["reason"] == "routing not requested"
    assert output.is_file()


def test_cli_gen_compose_missing_request_arg_exit_2():
    with pytest.raises(SystemExit) as excinfo:
        main(["gen-compose"])
    assert excinfo.value.code == 2


def test_cli_gen_compose_bad_format_exit_2():
    with pytest.raises(SystemExit) as excinfo:
        main(["gen-compose", "some.json", "--format", "bogus"])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# pins[] -- promote a single block port to a labelled top-level pin, no
# routing (#210). Every device gate (and any unrouted S/D terminal) that
# `connectivity[]` cannot express (it needs a 2-pin net) can be named this way.
# --------------------------------------------------------------------------- #


def test_compose_pins_absent_leaves_response_and_output_unchanged(tmp_path, pdk_root):
    # Omitting pins[] entirely must not change any existing behavior: the
    # response gains only an empty `pins` array, and no extra label is drawn.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    output = tmp_path / "no_pins.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "b1", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["b1"], "spacing_um": 1.0},
            "options": {"cell_name": "no_pins_0", "output": str(output)},
        }
    )
    assert report["pins"] == []
    assert report["nets"] == []
    assert report["drc_hints"]["notes"] == []


def test_compose_pins_rejects_unknown_block(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    with pytest.raises(GenComposeError, match="unknown block id"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": {"strategy": "row", "order": ["b1"], "spacing_um": 1.0},
                "pins": [{"net": "VB", "block": "nope", "port": "P1"}],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_pins_rejects_unknown_port(tmp_path, pdk_root):
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    with pytest.raises(GenComposeError, match="unknown port"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "b1", "generator_report": r1}],
                "placement": {"strategy": "row", "order": ["b1"], "spacing_um": 1.0},
                "pins": [{"net": "VB", "block": "b1", "port": "NOPE"}],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_pins_rejects_port_also_in_connectivity(tmp_path, pdk_root):
    # A (block, port) that connectivity[] already wires (and thus labels) may
    # not also be promoted by pins[] -- a second, possibly inconsistent label
    # on the same physical shape is ambiguous, not additive (exit 1).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    with pytest.raises(GenComposeError, match="already labelled by a connectivity"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "b1", "generator_report": r1},
                    {"id": "b2", "generator_report": r2},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["b1", "b2"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "b1", "port": "P2"},
                            {"block": "b2", "port": "P1"},
                        ],
                    }
                ],
                "routing": {"layer_role": "metal", "width_um": 0.17},
                "pins": [{"net": "N1_ALIAS", "block": "b1", "port": "P2"}],
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_compose_pins_labels_metal_port_at_its_own_position(tmp_path, pdk_root):
    # A pins[] entry on a metal port (resistor_strip P1, li1) is labelled on
    # li1.pin (67/5) at the port's own composed-frame position -- the port's
    # x_um/y_um plus its block's placement offset -- with no metal drawn.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r2")
    output = tmp_path / "pin_labelled.gds"
    r1_p1 = next(p for p in r1["ports"] if p["name"] == "P1")

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": r1},
                {"id": "b2", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
            "pins": [{"net": "VREF", "block": "b1", "port": "P1"}],
            "options": {"cell_name": "pin_labelled_0", "output": str(output)},
        }
    )
    assert report["pins"] == [
        {"net": "VREF", "block": "b1", "port": "P1", "labelled": True}
    ]

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("pin_labelled_0")
    li1_pin = layout.layer(67, 5)
    texts = list(top.shapes(li1_pin).each())
    assert len(texts) == 1
    text = texts[0]
    assert text.text.string == "VREF"
    # b1 is the first block (offset {0,0}), so the label sits at P1's own x/y.
    dbu = layout.dbu
    assert text.text.trans.disp.x * dbu == pytest.approx(r1_p1["x_um"], abs=dbu)
    assert text.text.trans.disp.y * dbu == pytest.approx(r1_p1["y_um"], abs=dbu)


def test_compose_pins_unmapped_layer_is_partial_success_note(tmp_path, pdk_root):
    # A pins[] entry whose port sits on a layer with no ExtractionDeck label
    # convention (here diff/active, 65/20 -- neither a `metals[]` entry nor the
    # deck's `poly` layer) is not labelled: reported as a drc_hints note
    # (partial success), not a hard failure. The block's own GDS is untouched;
    # only the port's reported layer drives label resolution, so retagging one
    # port in the report is a faithful stand-in for a generator whose port
    # genuinely lands on an unmapped layer.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r1")
    r1["ports"][0]["layer"] = {"layer": 65, "datatype": 20, "name": None}
    unmapped_port = r1["ports"][0]["name"]

    output = tmp_path / "pin_unmapped.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "g", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["g"], "spacing_um": 1.0},
            "pins": [{"net": "VSUB", "block": "g", "port": unmapped_port}],
            "options": {"cell_name": "pin_unmapped_0", "output": str(output)},
        }
    )
    assert report["pins"] == [
        {"net": "VSUB", "block": "g", "port": unmapped_port, "labelled": False}
    ]
    assert any(
        "no PDK label-layer convention" in note for note in report["drc_hints"]["notes"]
    )
    assert output.is_file()


def test_compose_pins_gate_port_survives_extraction_as_named_pin(tmp_path, pdk_root):
    # #210's acceptance bar: a device GATE -- which `klt gen` draws as bare
    # poly with no metal landing pad, so it is unrouteable/unlabelable by
    # connectivity[] and is demoted to an anonymous $N net today -- becomes a
    # NAMED .SUBCKT pin after `klt extract` once promoted via pins[]. Two
    # blocks are composed so this exercises the real multi-block path.
    tail = _gen_block(tmp_path, pdk_root, "mos_array", "tail", rows=1, cols=1)
    load = _gen_block(tmp_path, pdk_root, "mos_array", "load", rows=1, cols=1)

    output = tmp_path / "gate_pin.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "tail", "generator_report": tail},
                {"id": "load", "generator_report": load},
            ],
            "placement": {
                "strategy": "row",
                "order": ["tail", "load"],
                "spacing_um": 2.0,
            },
            "pins": [{"net": "VBIAS", "block": "tail", "port": "U0_G"}],
            "options": {"cell_name": "gate_pin_0", "output": str(output)},
        }
    )
    assert report["pins"] == [
        {"net": "VBIAS", "block": "tail", "port": "U0_G", "labelled": True}
    ]

    result = extract.run_extract(str(output), "sky130", top="gate_pin_0")
    pin_names = {net["name"] for net in result["nets"] if net["pin"]}
    # The gate node now comes back as a real, biasable pin -- not an anonymous
    # $N net (the friction #210 reports).
    assert "VBIAS" in pin_names


# --------------------------------------------------------------------------- #
# Ring routing openings (#434): a guard/collector ring generated with
# `ring_gap_side` reports a `GAP_<side>` opening, and a route that actually
# passes through that opening is allowed into an otherwise-ringed block. The
# closed-ring rejection above (#199 case 2) is unchanged.
# --------------------------------------------------------------------------- #


#: The ring gap #434's own repro needs: an opening on the side the route
#: leaves/enters through, slid onto the pair's lower device row (whose ports
#: sit 0.41um below the automatically-sized ring's own mid-height).
_DIFF_PAIR_GAP_E = {
    "ring_gap_side": "E",
    "ring_gap_um": 1.0,
    # Re-centre the E/W opening on the lower device row (the source/drain
    # ports' y) rather than the ring's own mid-height. core_h grew with the
    # gate landing pad's row-pitch bump (issue #461), so the centring offset
    # (ring mid-height minus the device-row y) grew with it.
    "ring_gap_offset_um": -0.83,
}
_DIFF_PAIR_GAP_W = dict(_DIFF_PAIR_GAP_E, ring_gap_side="W")


def _shares_merged_polygon(gds_path, cell_name, layer, datatype, p0_um, p1_um):
    """Whether the two points sit on the *same* merged polygon of one layer --
    i.e. whether they are electrically one net in the drawn geometry."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(gds_path))
    cell = layout.cell(cell_name)
    merged = kdb.Region(cell.begin_shapes_rec(layout.layer(layer, datatype))).merged()

    def _probe(point_um):
        x, y = (int(round(v / layout.dbu)) for v in point_um)
        return kdb.Region(kdb.Box(x - 1, y - 1, x + 1, y + 1))

    at_p0 = merged.interacting(_probe(p0_um))
    at_p1 = merged.interacting(_probe(p1_um))
    # Both probes must land on real metal for the answer to mean anything.
    assert not at_p0.is_empty(), f"no metal at {p0_um}"
    assert not at_p1.is_empty(), f"no metal at {p1_um}"
    return not at_p0.interacting(_probe(p1_um)).is_empty()


def _compose_two_diff_pairs(tmp_path, pdk_root, name, a_params, b_params):
    """#434's documented repro: a mirror-labelled pair wired to a plain pair's
    source, both with their default guard ring, differing only in the ring-gap
    params under test."""
    a = _gen_block(
        tmp_path, pdk_root, "diff_pair", f"a_{name}", mirror=True, splits=2, **a_params
    )
    b = _gen_block(
        tmp_path, pdk_root, "diff_pair", f"b_{name}", mirror=False, splits=2, **b_params
    )
    output = tmp_path / f"{name}.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "a", "generator_report": a},
                {"id": "b", "generator_report": b},
            ],
            "placement": {"strategy": "row", "order": ["a", "b"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "a", "port": "M1_1_D"},
                        {"block": "b", "port": "Q1_1_S"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": name, "output": str(output)},
        }
    )
    return a, report, output


def test_compose_routes_into_guard_ringed_block_through_a_declared_ring_gap(
    tmp_path, pdk_root
):
    # The exact case #434 filed: two diff_pairs, each keeping its default
    # guard ring, wired drain-to-source. With an opening declared on the side
    # each route leaves/enters through, the net routes instead of coming back
    # in unrouted_nets[].
    a_report, report, output = _compose_two_diff_pairs(
        tmp_path, pdk_root, "ringgap", _DIFF_PAIR_GAP_E, _DIFF_PAIR_GAP_W
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] > 0
    assert output.is_file()

    # ...and the drawn wire really goes *through* the opening: the routed
    # metal in the channel between the blocks is not part of the same merged
    # polygon as block a's guard ring (which would be exactly the short the
    # ring check exists to prevent).
    offsets = {b["id"]: b["offset_um"] for b in report["blocks"]}
    placed = {b["id"]: b["bbox_um"] for b in report["blocks"]}
    tap_n = next(p for p in a_report["ports"] if p["name"] == "TAP_N")
    route_point = (
        (placed["a"]["x1"] + placed["b"]["x0"]) / 2.0,
        0.21 + offsets["a"]["y"],
    )
    ring_point = (tap_n["x_um"] + offsets["a"]["x"], tap_n["y_um"] + offsets["a"]["y"])
    assert not _shares_merged_polygon(
        output, "ringgap", 67, 20, route_point, ring_point
    )


def test_compose_rejects_route_when_the_ring_gap_is_on_a_different_side(
    tmp_path, pdk_root
):
    # An opening exists, but not on the side this route crosses -- the ring is
    # still closed where the wire would go, so #199 case 2's protection holds.
    _, report, _ = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_wrongside",
        dict(_DIFF_PAIR_GAP_E, ring_gap_side="N"),
        _DIFF_PAIR_GAP_W,
    )
    assert report["unrouted_nets"] == ["N1"]
    assert any("declares no opening" in note for note in report["drc_hints"]["notes"])


def test_compose_rejects_route_that_misses_the_declared_ring_gap(tmp_path, pdk_root):
    # The opening is on the right side but left at the ring's own mid-height,
    # while the route crosses at the lower device row -- the wire would cut
    # the ring's metal, so the net is still reported unroutable.
    _, report, _ = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_missed",
        dict(_DIFF_PAIR_GAP_E, ring_gap_offset_um=0.0),
        _DIFF_PAIR_GAP_W,
    )
    assert report["unrouted_nets"] == ["N1"]
    assert any(
        "outside the" in note and "opening it declares" in note
        for note in report["drc_hints"]["notes"]
    )


def test_compose_ring_gap_too_narrow_for_the_route_width_is_rejected(
    tmp_path, pdk_root
):
    # A route needs half its width *plus* the block's own reported
    # min_spacing_um of clearance inside the opening -- an opening only just
    # wider than the wire would leave the wire shorted to the ring's cut ends.
    _, report, _ = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_narrow",
        dict(_DIFF_PAIR_GAP_E, ring_gap_um=0.4),
        _DIFF_PAIR_GAP_W,
    )
    assert report["unrouted_nets"] == ["N1"]
    assert any(
        "clearance inside the opening" in n for n in report["drc_hints"]["notes"]
    )


def test_compose_rejects_connectivity_to_a_ring_gap_port(tmp_path, pdk_root):
    # A GAP_* port marks the *absence* of metal -- wiring to it is an
    # application error, not a routable net.
    a = _gen_block(
        tmp_path, pdk_root, "diff_pair", "gapport_a", splits=1, **_DIFF_PAIR_GAP_E
    )
    b = _gen_block(
        tmp_path, pdk_root, "diff_pair", "gapport_b", splits=1, add_guard_ring=False
    )
    with pytest.raises(GenComposeError, match="marks a ring \\*opening\\*"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "a", "generator_report": a},
                    {"id": "b", "generator_report": b},
                ],
                "placement": {
                    "strategy": "row",
                    "order": ["a", "b"],
                    "spacing_um": 1.0,
                },
                "connectivity": [
                    {
                        "net": "N1",
                        "pins": [
                            {"block": "a", "port": "GAP_E"},
                            {"block": "b", "port": "Q1_1_S"},
                        ],
                    }
                ],
                "routing": {"layer_role": "metal", "width_um": 0.17},
                "options": {"output": str(tmp_path / "gapport.gds")},
            }
        )


def test_compose_rejects_pins_entry_naming_a_ring_gap_port(tmp_path, pdk_root):
    a = _gen_block(
        tmp_path, pdk_root, "diff_pair", "gappin_a", splits=1, **_DIFF_PAIR_GAP_E
    )
    with pytest.raises(GenComposeError, match="marks a ring \\*opening\\*"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "a", "generator_report": a}],
                "placement": {"strategy": "row", "order": ["a"], "spacing_um": 1.0},
                "pins": [{"net": "N1", "block": "a", "port": "GAP_E"}],
                "options": {"output": str(tmp_path / "gappin.gds")},
            }
        )


def test_compose_routes_into_collector_ringed_bjt_array_through_a_ring_gap(
    tmp_path, pdk_root
):
    # bjt_array's `add_collector_ring` (also on by default) is covered
    # symmetrically to diff_pair's `add_guard_ring`: its emitter ports face
    # north, so the opening goes on the ring's N side, and the partner block
    # is placed directly above with an explicit origin so the route is a
    # straight vertical line through the opening.
    bjt = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "bjt_gap",
        ring_gap_side="N",
        ring_gap_um=1.0,
        ring_gap_offset_um=-0.41,  # slides the opening onto Q0_E's own column
    )
    ring = _gen_block(tmp_path, pdk_root, "guard_ring", "bjt_partner")
    output = tmp_path / "bjt_ringgap.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "bjt", "generator_report": bjt},
                {"id": "ring", "generator_report": ring},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["bjt", "ring"],
                "origins_um": {
                    "bjt": {"x": 0.0, "y": 0.0},
                    # TAP_S sits at x=1.92 in the ring's own frame; Q0_E at
                    # x=2.12 in the array's -- so a 0.2um shift lines them up.
                    "ring": {"x": 0.2, "y": 5.0},
                },
            },
            "connectivity": [
                {
                    "net": "EMIT",
                    "pins": [
                        {"block": "bjt", "port": "Q0_E"},
                        {"block": "ring", "port": "TAP_S"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "bjt_ringgap", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert output.is_file()


def test_compose_rejects_route_into_collector_ringed_bjt_array_without_a_gap(
    tmp_path, pdk_root
):
    # The same composition with bjt_array's default *closed* collector ring is
    # still rejected -- the ring-gap path is additive, not a relaxation.
    bjt = _gen_block(tmp_path, pdk_root, "bjt_array", "bjt_closed")
    ring = _gen_block(tmp_path, pdk_root, "guard_ring", "bjt_closed_partner")
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "bjt", "generator_report": bjt},
                {"id": "ring", "generator_report": ring},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["bjt", "ring"],
                "origins_um": {
                    "bjt": {"x": 0.0, "y": 0.0},
                    "ring": {"x": 0.2, "y": 5.0},
                },
            },
            "connectivity": [
                {
                    "net": "EMIT",
                    "pins": [
                        {"block": "bjt", "port": "Q0_E"},
                        {"block": "ring", "port": "TAP_S"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {
                "cell_name": "bjt_closed_top",
                "output": str(tmp_path / "bjt_closed.gds"),
            },
        }
    )
    assert report["unrouted_nets"] == ["EMIT"]
    assert any(
        "closed guard/collector ring" in note for note in report["drc_hints"]["notes"]
    )


# --------------------------------------------------------------------------- #
# Stub-widen (#496): a north/south-facing port's *drawn* pad can be wider
# than the route's own `width_um` -- the un-widened stub still leaves the
# pad at `width_um`, so the pad's edge outside the stub's narrow footprint
# sits closer to the perpendicular jog above/below it than the target
# deck's same-layer spacing rule allows. `route_two_pin()` must widen just
# that stub segment (from the port out to where the un-widened stub already
# ends) to the port's own reported `width_um`, purely geometrically (no
# port-name special-casing) -- see `_endpoint_stub_widen_um`.
#
# `mos_array`'s `_G` port is used throughout (not a `metal` role -- #492's
# `gate_contact` param that promotes it is a *different*, not-yet-merged
# issue): its poly landing pad (issue #461, already on `main`) is exactly the
# gate-contact-shaped repro from the issue (a 0.42um-wide north-facing pad
# whose block bbox top sits *exactly* at the pad's own top, so the pad's own
# edge is what a narrower route leaves exposed) with no dependency on that
# unmerged param, and gf180mcu's curated deck checks `poly2.space.1` (sky130's
# does not check any poly spacing rule) -- so gf180mcu is where the *drawn*
# violation and its fix are both directly checkable via `klt drc`; sky130 is
# exercised alongside it as the required second deck (dual-deck parity,
# mirroring #454's own worked example) and as a "no new violation" regression
# even though it has nothing to catch here.
# --------------------------------------------------------------------------- #


def _mos_gate_bus_request(pdk_root, variant, m1, m2, width_um, output):
    return {
        "pdk": {"variant": variant, "root": str(pdk_root)},
        "blocks": [
            {"id": "a", "generator_report": m1},
            {"id": "b", "generator_report": m2},
        ],
        "placement": {"strategy": "row", "order": ["a", "b"], "spacing_um": 1.0},
        "connectivity": [
            {
                "net": "GNET",
                "pins": [
                    {"block": "a", "port": "U0_G"},
                    {"block": "b", "port": "U0_G"},
                ],
            }
        ],
        "routing": {"layer_role": "poly", "width_um": width_um},
        "options": {"cell_name": "gate_bus", "output": str(output)},
    }


@pytest.mark.parametrize(
    ("variant", "family", "width_um"),
    [
        # gf180mcu's poly2.space.1 (0.24um) is what actually catches the
        # pre-fix gap (poly2.width.1 is 0.18um, so 0.2 clears that separately).
        ("gf180mcuD", "gf180mcu", 0.2),
        # sky130's curated deck checks no poly spacing rule at all -- this
        # exercises the same fix on the second deck (dual-deck parity) and
        # confirms it introduces no *other* violation there, even though
        # sky130 has nothing to catch on this specific layer.
        ("sky130A", "sky130", 0.17),
    ],
)
def test_compose_widens_stub_beside_a_wide_gate_pad_is_drc_clean(
    tmp_path, both_pdk_root, variant, family, width_um
):
    m1 = _gen_block_variant(
        tmp_path,
        both_pdk_root,
        variant,
        "mos_array",
        f"m1_{family}",
        rows=1,
        cols=1,
        fingers=1,
        dummy=0,
    )
    m2 = _gen_block_variant(
        tmp_path,
        both_pdk_root,
        variant,
        "mos_array",
        f"m2_{family}",
        rows=1,
        cols=1,
        fingers=1,
        dummy=0,
    )
    output = tmp_path / f"gate_bus_{family}.gds"
    report = compose(
        _mos_gate_bus_request(both_pdk_root, variant, m1, m2, width_um, output)
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True

    drc_report = run_drc(str(output), family)
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_compose_gf180mcu_gate_pad_gap_is_a_real_pre_fix_violation(
    tmp_path, both_pdk_root
):
    # Companion to the DRC-clean test above: proves the gf180mcu case is a
    # real, *pre-existing* violation the fix closes, not a scenario that was
    # already clean regardless -- with `_endpoint_stub_widen_um` disabled
    # (simulating pre-#496 behavior), `klt drc --deck gf180mcu` reports
    # exactly the `poly2.space.1` gap the issue describes.
    m1 = _gen_block_variant(
        tmp_path,
        both_pdk_root,
        "gf180mcuD",
        "mos_array",
        "m1_prefix",
        rows=1,
        cols=1,
        fingers=1,
        dummy=0,
    )
    m2 = _gen_block_variant(
        tmp_path,
        both_pdk_root,
        "gf180mcuD",
        "mos_array",
        "m2_prefix",
        rows=1,
        cols=1,
        fingers=1,
        dummy=0,
    )
    output = tmp_path / "gate_bus_prefix.gds"

    orig = gen_compose._endpoint_stub_widen_um
    gen_compose._endpoint_stub_widen_um = lambda *args, **kwargs: None
    try:
        report = compose(
            _mos_gate_bus_request(both_pdk_root, "gf180mcuD", m1, m2, 0.2, output)
        )
    finally:
        gen_compose._endpoint_stub_widen_um = orig

    assert report["nets"][0]["routed"] is True
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "violations"
    assert any(v["rule"] == "poly2.space.1" for v in drc_report["violations"])


def test_compose_stub_widen_is_a_no_op_when_route_width_already_matches_the_pad(
    tmp_path, pdk_root
):
    # Edge case: routing.width_um equal to the port's own reported width_um
    # (the pad's width) leaves no gap to close -- no widen box is drawn, and
    # the drawn geometry is exactly the plain backbone Path.
    m1 = _gen_block(
        tmp_path, pdk_root, "mos_array", "m1_eq", rows=1, cols=1, fingers=1, dummy=0
    )
    m2 = _gen_block(
        tmp_path, pdk_root, "mos_array", "m2_eq", rows=1, cols=1, fingers=1, dummy=0
    )
    pad_width_um = next(p["width_um"] for p in m1["ports"] if p["name"] == "U0_G")
    output = tmp_path / "gate_bus_eq.gds"
    report = compose(
        _mos_gate_bus_request(pdk_root, "sky130A", m1, m2, pad_width_um, output)
    )
    assert report["nets"][0]["routed"] is True

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("gate_bus")
    poly = layout.layer(66, 20)
    shapes = list(top.shapes(poly).each())
    assert len(shapes) == 1  # only the backbone Path -- no widen box added
    assert shapes[0].is_path()


def test_compose_stub_widen_draws_the_pad_width_box_beside_each_endpoint(
    tmp_path, pdk_root
):
    # Direct geometry check (deck-independent of any particular DRC rule):
    # each endpoint whose pad is wider than the route gets one extra Box, on
    # the route layer, spanning from that endpoint's own position out to
    # where the (un-widened) stub already ends, at the pad's own width --
    # not the route's.
    m1 = _gen_block(
        tmp_path, pdk_root, "mos_array", "m1_geo", rows=1, cols=1, fingers=1, dummy=0
    )
    m2 = _gen_block(
        tmp_path, pdk_root, "mos_array", "m2_geo", rows=1, cols=1, fingers=1, dummy=0
    )
    g_port = next(p for p in m1["ports"] if p["name"] == "U0_G")
    pad_width_um = g_port["width_um"]
    route_width_um = 0.17
    assert pad_width_um > route_width_um  # precondition: this must actually widen

    output = tmp_path / "gate_bus_geo.gds"
    compose(_mos_gate_bus_request(pdk_root, "sky130A", m1, m2, route_width_um, output))

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("gate_bus")
    poly = layout.layer(66, 20)
    boxes = [s.box for s in top.shapes(poly).each() if s.is_box()]
    paths = [s for s in top.shapes(poly).each() if s.is_path()]
    assert len(boxes) == 2  # one widened stub per endpoint
    assert len(paths) == 1  # the plain (narrow) backbone, unchanged

    dbu = layout.dbu
    pad_half_dbu = int(round((pad_width_um / 2.0) / dbu))
    for box in boxes:
        assert box.width() == pytest.approx(2 * pad_half_dbu, abs=1)


def test_route_two_pin_reports_no_stub_widen_for_east_west_facing_ports(
    tmp_path, pdk_root
):
    # Regression (east/west unaffected, per the issue's own scope): an S/D
    # port's real drawn pad is also wider than a narrow route (reported
    # `width_um` is the diffusion height), but its facing direction is
    # east/west -- `stub_widen` must stay empty, so the drawn geometry is
    # byte-for-byte the same as before #496.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "m1_ew", rows=1, cols=1)
    m2 = _gen_block(tmp_path, pdk_root, "mos_array", "m2_ew", rows=1, cols=1)
    blocks = gen_compose._parse_blocks(
        [
            {"id": "a", "generator_report": m1},
            {"id": "b", "generator_report": m2},
        ]
    )
    offsets = {"a": {"x": 0.0, "y": 0.0}, "b": {"x": 3.0, "y": 0.0}}
    bboxes = {
        "a": gen_compose._translate_bbox(blocks["a"]["bbox_um"], offsets["a"]),
        "b": gen_compose._translate_bbox(blocks["b"]["bbox_um"], offsets["b"]),
    }
    route_layer = gen_compose._resolve_route_layer("sky130A", "metal")
    result = gen_compose.route_two_pin(
        {"block": "a", "port": "U0_D"},
        {"block": "b", "port": "U0_S"},
        blocks,
        offsets,
        bboxes,
        0.17,
        route_layer,
    )
    assert result["routed"] is True
    assert result["stub_widen"] == []


def test_route_two_pin_reports_no_stub_widen_when_the_pad_needs_a_via_drop(
    tmp_path, pdk_root
):
    # A north-facing gate port's own layer (poly) differs from the resolved
    # route_layer (metal) -- the port's real pad lives on a different layer
    # entirely, so widening metal there would not correspond to any drawn
    # geometry; stub_widen must stay empty regardless of how wide the pad's
    # own reported width_um is.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "m1_layer", rows=1, cols=1)
    m2 = _gen_block(tmp_path, pdk_root, "mos_array", "m2_layer", rows=1, cols=1)
    blocks = gen_compose._parse_blocks(
        [
            {"id": "a", "generator_report": m1},
            {"id": "b", "generator_report": m2},
        ]
    )
    offsets = {"a": {"x": 0.0, "y": 0.0}, "b": {"x": 3.0, "y": 0.0}}
    bboxes = {
        "a": gen_compose._translate_bbox(blocks["a"]["bbox_um"], offsets["a"]),
        "b": gen_compose._translate_bbox(blocks["b"]["bbox_um"], offsets["b"]),
    }
    route_layer = gen_compose._resolve_route_layer("sky130A", "metal")
    result = gen_compose.route_two_pin(
        {"block": "a", "port": "U0_G"},
        {"block": "b", "port": "U0_G"},
        blocks,
        offsets,
        bboxes,
        0.17,
        route_layer,
    )
    assert result["routed"] is True
    assert result["stub_widen"] == []


@pytest.mark.parametrize(
    ("pad_width_um", "route_width_um", "expect_widen"),
    [
        (0.42, 0.17, True),  # pad wider than route -- widens
        (0.42, 0.42, False),  # equal -- no-op (no gap to close)
        (0.42, 0.5, False),  # route already wider than the pad -- no-op
        (0.20, 0.17, True),  # marginally wider -- still widens
    ],
)
def test_endpoint_stub_widen_um_thresholds(pad_width_um, route_width_um, expect_widen):
    port = {
        "width_um": pad_width_um,
        "layer": {"layer": 67, "datatype": 20},
    }
    route_layer = (67, 20)
    widen = gen_compose._endpoint_stub_widen_um(
        port, (1.0, 2.0), 90, 0.3, route_width_um, route_layer
    )
    if expect_widen:
        assert widen == {
            "x_um": 1.0,
            "y_um": 2.0,
            "direction_deg": 90,
            "length_um": 0.3,
            "width_um": pad_width_um,
        }
    else:
        assert widen is None


@pytest.mark.parametrize("direction_deg", [0, 180])
def test_endpoint_stub_widen_um_ignores_east_west_directions(direction_deg):
    port = {"width_um": 0.42, "layer": {"layer": 67, "datatype": 20}}
    assert (
        gen_compose._endpoint_stub_widen_um(
            port, (1.0, 2.0), direction_deg, 0.3, 0.17, (67, 20)
        )
        is None
    )


def test_endpoint_stub_widen_um_ignores_a_different_layer_port():
    # The port's own reported layer differs from route_layer (the shape of a
    # via-drop endpoint) -- no widen, regardless of a wide reported width_um.
    port = {"width_um": 0.42, "layer": {"layer": 66, "datatype": 20}}
    assert (
        gen_compose._endpoint_stub_widen_um(port, (1.0, 2.0), 90, 0.3, 0.17, (67, 20))
        is None
    )


def test_endpoint_stub_widen_um_requires_a_route_layer():
    port = {"width_um": 0.42, "layer": {"layer": 67, "datatype": 20}}
    assert (
        gen_compose._endpoint_stub_widen_um(port, (1.0, 2.0), 90, 0.3, 0.17, None)
        is None
    )


# --------------------------------------------------------------------------- #
# blocks[].orientation (#1166): mirroring unblocks a same-facing shared net --
# the parent friction report's minimal two-`mos_array` "CMOS inverter" case
# (#1164 root cause #1): "source-left/drain-right in a fixed orientation, so
# two blocks that need to face each other to route their shared net ... cannot
# be made to face each other." `mos_array` (rows=1, cols=1) reports `U0_S` on
# its own left edge (180deg) and `U0_D` on its own right edge (0deg) -- see
# gen.py's `_mos_array_describe` -- so two such blocks placed side by side in
# a row have their drains on the *same* absolute side (both facing +x, away
# from each other on the far block), reproducing the report's root cause.
# --------------------------------------------------------------------------- #


def _same_facing_drain_request(m1, m2, pdk_root, output, p_orientation=None):
    p_block = {"id": "p", "generator_report": m2}
    if p_orientation is not None:
        p_block["orientation"] = p_orientation
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": "n", "generator_report": m1}, p_block],
        "placement": {"strategy": "row", "order": ["n", "p"], "spacing_um": 1.0},
        "connectivity": [
            {
                "net": "VOUT",
                "pins": [
                    {"block": "n", "port": "U0_D"},
                    {"block": "p", "port": "U0_D"},
                ],
            }
        ],
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {"cell_name": "inverter_0", "output": str(output)},
    }


def test_compose_same_facing_drain_net_is_unrouted_without_orientation(
    tmp_path, pdk_root
):
    # Baseline (pre-#1166, reproducing #1164's root cause #1): "p"'s own D is
    # on p's *far* side from "n" -- wiring n.D to p.D forces the backbone
    # through p's own body, which route_two_pin's obstacle-overlap check
    # (check 5) rejects.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "nfet_like", rows=1, cols=1)
    m2 = _gen_block(tmp_path, pdk_root, "mos_array", "pfet_like", rows=1, cols=1)
    output = tmp_path / "inverter_unrouted.gds"
    report = compose(_same_facing_drain_request(m1, m2, pdk_root, output))

    assert report["unrouted_nets"] == ["VOUT"]
    assert report["nets"][0]["routed"] is False


def test_compose_mirrored_block_routes_same_facing_drain_net_and_is_drc_clean(
    tmp_path, pdk_root
):
    # #1166 fix: mirroring "p" ("mirror_x") moves its own U0_D from its right
    # edge (facing +x, away from "n") to its left edge (facing -x, toward
    # "n") -- the two drains now face each other directly across the row's
    # spacing_um channel, and the net routes as a plain straight backbone.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "nfet_like2", rows=1, cols=1)
    m2 = _gen_block(tmp_path, pdk_root, "mos_array", "pfet_like2", rows=1, cols=1)
    output = tmp_path / "inverter_mirrored.gds"
    report = compose(
        _same_facing_drain_request(m1, m2, pdk_root, output, p_orientation="mirror_x")
    )

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["blocks"][0]["orientation"] == "none"
    assert report["blocks"][1]["orientation"] == "mirror_x"

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]

    # Loop closure: klt extract still sees VOUT as one named net joining
    # both blocks' drains, not two disconnected pins.
    result = extract.run_extract(str(output), "sky130", top="inverter_0")
    vout_pin_nets = {net["name"] for net in result["nets"] if net.get("pin")}
    assert "VOUT" in vout_pin_nets


def test_compose_mirrored_block_drawn_geometry_matches_reported_bbox(
    tmp_path, pdk_root
):
    # #1166 acceptance criteria: a mirrored block's *drawn* geometry (the
    # actual GDS shapes _write_composed_gds inserts) must land exactly where
    # its reported bbox_um says -- no metadata/geometry mismatch.
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "geom_mirrored", rows=1, cols=1)
    output = tmp_path / "geom_mirrored.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "a", "generator_report": m1, "orientation": "mirror_x"}],
            "placement": {"strategy": "row", "order": ["a"], "spacing_um": 1.0},
            "options": {"cell_name": "geom_mirrored_0", "output": str(output)},
        }
    )
    reported_bbox = report["blocks"][0]["bbox_um"]

    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("geom_mirrored_0")
    drawn_bbox = top.bbox()
    dbu = layout.dbu
    assert drawn_bbox.left * dbu == pytest.approx(reported_bbox["x0"], abs=2 * dbu)
    assert drawn_bbox.bottom * dbu == pytest.approx(reported_bbox["y0"], abs=2 * dbu)
    assert drawn_bbox.right * dbu == pytest.approx(reported_bbox["x1"], abs=2 * dbu)
    assert drawn_bbox.top * dbu == pytest.approx(reported_bbox["y1"], abs=2 * dbu)


def test_compose_orientation_combines_with_explicit_placement_offset(
    tmp_path, pdk_root
):
    # Edge case (#1166 test plan): orientation composes with an explicit
    # per-block offset_um -- the block is mirrored about its own local origin
    # first, then translated by the declared origin (never the other way
    # round), exactly as _apply_orientation_um's docstring states.
    m1 = _gen_block(
        tmp_path, pdk_root, "mos_array", "explicit_mirrored", rows=1, cols=1
    )
    output = tmp_path / "explicit_mirrored.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "a", "generator_report": m1, "orientation": "mirror_x"}],
            "placement": {
                "strategy": "explicit",
                "order": ["a"],
                "origins_um": {"a": {"x": 10.0, "y": 20.0}},
            },
            "options": {"cell_name": "explicit_mirrored_0", "output": str(output)},
        }
    )

    orig_bbox = m1["bbox_um"]
    placed = report["blocks"][0]["bbox_um"]
    assert placed["x1"] - placed["x0"] == pytest.approx(
        orig_bbox["x1"] - orig_bbox["x0"]
    )
    assert placed["y1"] - placed["y0"] == pytest.approx(
        orig_bbox["y1"] - orig_bbox["y0"]
    )
    mirrored_local = _orient_bbox_um(orig_bbox, "mirror_x")
    assert placed["x0"] == pytest.approx(mirrored_local["x0"] + 10.0)
    assert placed["y0"] == pytest.approx(mirrored_local["y0"] + 20.0)


def test_compose_rejects_unsupported_block_orientation(tmp_path, pdk_root):
    block = _gen_block(tmp_path, pdk_root, "resistor_strip", "r0")
    with pytest.raises(GenComposeError, match="orientation"):
        compose(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "r", "generator_report": block, "orientation": "upside_down"}
                ],
                "placement": {"strategy": "row", "order": ["r"], "spacing_um": 1.0},
            }
        )


# --------------------------------------------------------------------------- #
# blocks[].cell -- placing a cell this command did not generate (#1189)
# --------------------------------------------------------------------------- #


def _write_library_gds(path, cells):
    """Fabricate a multi-cell "library" stream -- a stand-in for a PDK's own
    standard-cell GDS, i.e. exactly the case #1189 filed: a cell no `klt` verb
    ever generated, so there is no `generator_report` to feed `blocks[]`.

    ``cells`` maps a cell name to its ``(width_um, height_um)``. Each cell
    draws one solid li1 (67/20) rectangle with its lower-left corner at the
    cell origin, so its `dbbox()` is exactly ``(0, 0) - (width, height)`` and
    a caller can predict what `read_cell_bbox_um` must report.
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    li1 = layout.layer(67, 20)
    for name, (width_um, height_um) in cells.items():
        cell = layout.create_cell(name)
        cell.shapes(li1).insert(
            kdb.Box(
                0,
                0,
                int(round(width_um / layout.dbu)),
                int(round(height_um / layout.dbu)),
            )
        )
    layout.write(str(path))
    return str(path)


def _library_cell_ports(width_um, height_um):
    """Two edge ports on the li1 rectangle `_write_library_gds` draws -- an
    input on the left edge (facing 180) and an output on the right (facing 0),
    the same shape `klt gen`'s own `ports[]` entries use."""
    return [
        {
            "name": "A",
            "layer": {"layer": 67, "datatype": 20},
            "x_um": 0.0,
            "y_um": height_um / 2.0,
            "width_um": 0.2,
            "direction_deg": 180,
        },
        {
            "name": "Y",
            "layer": {"layer": 67, "datatype": 20},
            "x_um": width_um,
            "y_um": height_um / 2.0,
            "width_um": 0.2,
            "direction_deg": 0,
        },
    ]


def test_compose_cell_block_reads_bbox_from_the_stream(tmp_path, pdk_root):
    # #1189's library-cell case: a `blocks[].cell` entry naming a cell that
    # already exists in a stream needs no bbox_um at all -- the composer reads
    # it off the cell itself, so the caller never re-keys `klt cells`'
    # {left, bottom, right, top} into gen's {x0, y0, x1, y1}.
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    output = tmp_path / "lib_placed.gds"

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "u1", "cell": {"gds_path": gds, "cell_name": "lib_inv"}}],
            "placement": {"strategy": "row", "order": ["u1"], "spacing_um": 1.0},
            "options": {"cell_name": "lib_placed_0", "output": str(output)},
        }
    )

    assert report["blocks"][0]["source"] == "cell"
    assert report["blocks"][0]["generator"] is None
    assert report["blocks"][0]["cell_name"] == "lib_inv"
    assert report["blocks"][0]["bbox_um"] == pytest.approx(
        {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.2}
    )
    assert report["bbox_um"] == pytest.approx(
        {"x0": 0.0, "y0": 0.0, "x1": 2.0, "y1": 1.2}
    )

    # The library cell's own geometry really was copied into the composed top.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("lib_placed_0")
    assert len(list(top.each_inst())) == 1
    assert top.dbbox().right == pytest.approx(2.0)


def test_compose_cell_block_declared_bbox_um_is_used_verbatim(tmp_path, pdk_root):
    # A caller who *does* know the cell's placement footprint (e.g. a standard
    # cell whose row-abutment box is larger than its drawn shapes) may declare
    # bbox_um; the stream is then not consulted for it at all.
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    output = tmp_path / "lib_declared.gds"

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {
                    "id": "u1",
                    "cell": {
                        "gds_path": gds,
                        "cell_name": "lib_inv",
                        "bbox_um": {"x0": -0.5, "y0": -0.5, "x1": 3.0, "y1": 2.0},
                    },
                }
            ],
            "placement": {"strategy": "row", "order": ["u1"], "spacing_um": 1.0},
            "options": {"cell_name": "lib_declared_0", "output": str(output)},
        }
    )
    assert report["blocks"][0]["bbox_um"] == pytest.approx(
        {"x0": -0.5, "y0": -0.5, "x1": 3.0, "y1": 2.0}
    )


def test_compose_cell_block_ports_route_like_any_other_block(tmp_path, pdk_root):
    # The other half of #1189's library-cell case: a `cell` block's declared
    # ports[] participate in connectivity[] exactly as a generated block's
    # reported ones do -- so a row of library cells can actually be wired,
    # not merely placed.
    gds = _write_library_gds(
        tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2), "lib_buf": (1.5, 1.2)}
    )
    output = tmp_path / "lib_routed.gds"

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {
                    "id": "u1",
                    "cell": {
                        "gds_path": gds,
                        "cell_name": "lib_inv",
                        "ports": _library_cell_ports(2.0, 1.2),
                    },
                },
                {
                    "id": "u2",
                    "cell": {
                        "gds_path": gds,
                        "cell_name": "lib_buf",
                        "ports": _library_cell_ports(1.5, 1.2),
                    },
                },
            ],
            "placement": {"strategy": "row", "order": ["u1", "u2"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "N1",
                    "pins": [
                        {"block": "u1", "port": "Y"},
                        {"block": "u2", "port": "A"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "lib_routed_0", "output": str(output)},
        }
    )

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert report["nets"][0]["route_length_um"] == pytest.approx(1.0)
    assert output.is_file()


def test_compose_cell_block_orientation_mirrors_bbox_and_ports(tmp_path, pdk_root):
    # blocks[].orientation (#1166) is a per-block placement attribute, so it
    # applies to a `cell` block identically -- including to a bbox that was
    # read from the stream rather than declared.
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    blocks = _parse_blocks(
        [
            {
                "id": "u1",
                "cell": {
                    "gds_path": gds,
                    "cell_name": "lib_inv",
                    "ports": _library_cell_ports(2.0, 1.2),
                },
                "orientation": "mirror_x",
            }
        ]
    )
    block = blocks["u1"]
    assert block["source"] == "cell"
    assert block["bbox_um"] == pytest.approx(
        {"x0": -2.0, "y0": 0.0, "x1": 0.0, "y1": 1.2}
    )
    assert block["ports"]["Y"]["x_um"] == pytest.approx(-2.0)
    assert block["ports"]["Y"]["direction_deg"] == 180


def test_compose_cell_block_relative_gds_path_resolves_against_request_dir(
    tmp_path, pdk_root, monkeypatch
):
    # A relative cell.gds_path resolves against the request document's own
    # directory, matching blocks[].generator_report's own path convention --
    # not the process's cwd.
    request_dir = tmp_path / "req"
    request_dir.mkdir()
    _write_library_gds(request_dir / "lib.gds", {"lib_inv": (2.0, 1.2)})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "u1", "cell": {"gds_path": "lib.gds", "cell_name": "lib_inv"}}
            ],
            "placement": {"strategy": "row", "order": ["u1"], "spacing_um": 1.0},
            "options": {
                "cell_name": "rel_0",
                "output": str(tmp_path / "rel_0.gds"),
            },
        },
        request_dir=str(request_dir),
    )
    assert report["blocks"][0]["bbox_um"]["x1"] == pytest.approx(2.0)


def test_compose_cell_block_unknown_cell_name_lists_what_is_available(tmp_path):
    gds = _write_library_gds(
        tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2), "lib_buf": (1.5, 1.2)}
    )
    with pytest.raises(GenComposeError) as excinfo:
        _parse_blocks(
            [{"id": "u1", "cell": {"gds_path": gds, "cell_name": "lib_nand"}}]
        )
    message = str(excinfo.value)
    assert "has no cell named 'lib_nand'" in message
    assert "lib_buf" in message and "lib_inv" in message


def test_compose_cell_block_empty_cell_bbox_is_an_application_error(tmp_path):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.create_cell("hollow")
    gds = tmp_path / "hollow.gds"
    layout.write(str(gds))

    with pytest.raises(GenComposeError, match="declare cell.bbox_um"):
        _parse_blocks(
            [{"id": "u1", "cell": {"gds_path": str(gds), "cell_name": "hollow"}}]
        )


def test_compose_cell_block_unreadable_stream_is_an_application_error(tmp_path):
    with pytest.raises(GenComposeError, match="could not read cell.gds_path"):
        _parse_blocks(
            [
                {
                    "id": "u1",
                    "cell": {
                        "gds_path": str(tmp_path / "nope.gds"),
                        "cell_name": "x",
                    },
                }
            ]
        )


def test_parse_blocks_rejects_both_generator_report_and_cell(tmp_path):
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    with pytest.raises(GenComposeError, match="exactly one source of geometry"):
        _parse_blocks(
            [
                {
                    "id": "u1",
                    "generator_report": _fake_mos_block_report(),
                    "cell": {"gds_path": gds, "cell_name": "lib_inv"},
                }
            ]
        )


def test_parse_blocks_rejects_block_with_neither_source():
    with pytest.raises(GenComposeError, match="must declare either"):
        _parse_blocks([{"id": "u1"}])


def test_parse_blocks_generator_missing_error_points_at_the_cell_form():
    # The hand-forged-report workaround #1189 documents ("write a fake
    # generator field") must be answered by the error itself, not by the docs
    # alone: the caller is told the supported form for a cell no klt verb
    # generated.
    with pytest.raises(GenComposeError, match=r"use blocks\[\]\.cell"):
        _parse_blocks(
            [
                {
                    "id": "u1",
                    "generator_report": {
                        "cell_name": "lib_inv",
                        "gds_path": "lib.gds",
                        "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
                    },
                }
            ]
        )


@pytest.mark.parametrize(
    ("ports", "match"),
    [
        ("not-an-array", "ports must be an array"),
        ([["A"]], "must be a JSON object"),
        ([{"x_um": 0.0}], "name is required"),
        ([{"name": "A"}, {"name": "A"}], "duplicate name 'A'"),
        ([{"name": "A", "x_um": 0.0}], "both x_um and y_um"),
        ([{"name": "A", "x_um": 0.0, "y_um": 0.0, "width_um": 0}], "width_um"),
        (
            [{"name": "A", "x_um": 0.0, "y_um": 0.0, "direction_deg": 45}],
            "direction_deg",
        ),
        ([{"name": "A", "layer": [67, 20]}], "layer must be a JSON object"),
    ],
)
def test_compose_cell_block_rejects_malformed_ports(tmp_path, ports, match):
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    with pytest.raises(GenComposeError, match=match):
        _parse_blocks(
            [
                {
                    "id": "u1",
                    "cell": {
                        "gds_path": gds,
                        "cell_name": "lib_inv",
                        "ports": ports,
                    },
                }
            ]
        )


def test_compose_cell_block_port_direction_deg_normalises_to_int(tmp_path):
    # A JSON document may carry 180.0 where 180 was meant; the orientation
    # remap table (_ORIENTATION_DIRECTION_MAP) is keyed on int, so the parsed
    # port must be too.
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    blocks = _parse_blocks(
        [
            {
                "id": "u1",
                "cell": {
                    "gds_path": gds,
                    "cell_name": "lib_inv",
                    "ports": [
                        {
                            "name": "A",
                            "x_um": 0.0,
                            "y_um": 0.6,
                            "direction_deg": 180.0,
                            "layer": {"layer": 67, "datatype": 20},
                        }
                    ],
                },
                "orientation": "mirror_x",
            }
        ]
    )
    assert blocks["u1"]["ports"]["A"]["direction_deg"] == 0


# --------------------------------------------------------------------------- #
# Hierarchical composition -- a gen-compose response as a block (#1189)
# --------------------------------------------------------------------------- #


def _compose_pair(tmp_path, pdk_root, name, spacing_um=2.0):
    """Compose two `resistor_strip` blocks into one cell, promoting the outer
    P1/P2 terminals to top-level `IN`/`OUT` pins -- the child composition every
    hierarchical test below then places one level up."""
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", f"{name}_r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", f"{name}_r2")
    return compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "r1", "generator_report": r1},
                {"id": "r2", "generator_report": r2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["r1", "r2"],
                "spacing_um": spacing_um,
            },
            "connectivity": [
                {
                    "net": "MID",
                    "pins": [
                        {"block": "r1", "port": "P2"},
                        {"block": "r2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "pins": [
                {"net": "IN", "block": "r1", "port": "P1"},
                {"net": "OUT", "block": "r2", "port": "P2"},
            ],
            "options": {
                "cell_name": name,
                "output": str(tmp_path / f"{name}.gds"),
            },
        }
    )


def test_compose_response_is_itself_a_valid_generator_report(tmp_path, pdk_root):
    # #1189 direction 1: the response carries the `generator` marker
    # _parse_blocks requires plus a ports[] promoted from its own pins[], so
    # feeding it straight back in needs no hand-patched fields.
    child = _compose_pair(tmp_path, pdk_root, "child_0")

    assert child["generator"] == "gen-compose"
    assert [p["name"] for p in child["ports"]] == ["IN", "OUT"]
    assert {p["net"] for p in child["ports"]} == {"IN", "OUT"}

    # Every promoted port is in the *composed* frame: its sub-block port's own
    # position plus that sub-block's placement offset.
    offsets = {b["id"]: b["offset_um"] for b in child["blocks"]}
    out_port = next(p for p in child["ports"] if p["name"] == "OUT")
    assert out_port["block"] == "r2"
    assert out_port["port"] == "P2"
    assert out_port["x_um"] == pytest.approx(child["bbox_um"]["x1"])
    assert out_port["x_um"] > offsets["r2"]["x"]
    assert out_port["direction_deg"] == 0
    assert out_port["layer"] == {"layer": 67, "datatype": 20, "name": None}

    # The whole response satisfies _parse_blocks with nothing added.
    blocks = _parse_blocks([{"id": "sub", "generator_report": child}])
    assert blocks["sub"]["generator"] == "gen-compose"
    assert blocks["sub"]["source"] == "generator_report"
    assert set(blocks["sub"]["port_names"]) == {"IN", "OUT"}


def test_compose_nests_a_composed_cell_and_routes_its_promoted_port(tmp_path, pdk_root):
    # The payoff: a second level of composition places the child composition as
    # one block and wires its promoted top-level port to a further block --
    # what "no hierarchical composition, only one flat level" (#1189) blocked.
    child = _compose_pair(tmp_path, pdk_root, "child_1")
    r3 = _gen_block(tmp_path, pdk_root, "resistor_strip", "r3")
    output = tmp_path / "parent_1.gds"

    parent = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "sub", "generator_report": child},
                {"id": "r3", "generator_report": r3},
            ],
            "placement": {
                "strategy": "row",
                "order": ["sub", "r3"],
                "spacing_um": 2.0,
            },
            "connectivity": [
                {
                    "net": "CHAIN",
                    "pins": [
                        {"block": "sub", "port": "OUT"},
                        {"block": "r3", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "parent_1", "output": str(output)},
        }
    )

    assert parent["unrouted_nets"] == []
    assert parent["nets"][0]["routed"] is True
    assert parent["blocks"][0]["generator"] == "gen-compose"
    assert parent["blocks"][0]["cell_name"] == "child_1"
    # The parent's own extent starts from the child's full composed bbox --
    # the nested cell is placed whole, not just its first sub-block.
    assert parent["blocks"][0]["bbox_um"] == pytest.approx(child["bbox_um"])

    # Hierarchy survives: the parent top instantiates two cells, and the
    # child's own sub-cells came along with it.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("parent_1")
    assert len(list(top.each_inst())) == 2
    cell_names = {cell.name for cell in layout.each_cell()}
    assert "sub__child_1" in cell_names
    assert any(name.startswith("r1__") for name in cell_names)


def test_compose_nested_composition_can_re_promote_a_port(tmp_path, pdk_root):
    # A promoted port stays promotable one level further up: the parent's own
    # pins[] may name it, so a three-level hierarchy keeps its top-level nets.
    child = _compose_pair(tmp_path, pdk_root, "child_2")
    parent = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "sub", "generator_report": child}],
            "placement": {
                "strategy": "explicit",
                "order": ["sub"],
                "origins_um": {"sub": {"x": 5.0, "y": 3.0}},
            },
            "pins": [{"net": "TOP_IN", "block": "sub", "port": "IN"}],
            "options": {
                "cell_name": "parent_2",
                "output": str(tmp_path / "parent_2.gds"),
            },
        }
    )
    child_in = next(p for p in child["ports"] if p["name"] == "IN")
    assert parent["pins"] == [
        {"net": "TOP_IN", "block": "sub", "port": "IN", "labelled": True}
    ]
    top_in = next(p for p in parent["ports"] if p["name"] == "TOP_IN")
    assert top_in["x_um"] == pytest.approx(child_in["x_um"] + 5.0)
    assert top_in["y_um"] == pytest.approx(child_in["y_um"] + 3.0)


def test_compose_ports_is_empty_without_pins(tmp_path, pdk_root):
    # Backward compatibility: a request that promotes nothing exposes nothing.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "solo_ports")
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "solo", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["solo"], "spacing_um": 1.0},
            "options": {
                "cell_name": "solo_ports_0",
                "output": str(tmp_path / "solo_ports_0.gds"),
            },
        }
    )
    assert report["generator"] == "gen-compose"
    assert report["ports"] == []


def test_compose_promotes_a_port_whose_layer_has_no_label_convention(
    tmp_path, pdk_root
):
    # A port on a layer `klt extract` has no label convention for is reported
    # `labelled: false` -- but it still has a position, so it is still a real
    # port of the composed cell and must be promoted. Routing at the level
    # above needs geometry, not a label.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "unmapped_promote")
    r1["ports"][0]["layer"] = {"layer": 65, "datatype": 20, "name": None}
    unmapped_port = r1["ports"][0]["name"]

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "g", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["g"], "spacing_um": 1.0},
            "pins": [{"net": "VSUB", "block": "g", "port": unmapped_port}],
            "options": {
                "cell_name": "unmapped_promote_0",
                "output": str(tmp_path / "unmapped_promote_0.gds"),
            },
        }
    )
    assert report["pins"][0]["labelled"] is False
    assert [p["name"] for p in report["ports"]] == ["VSUB"]
    assert report["ports"][0]["layer"] == {"layer": 65, "datatype": 20, "name": None}


def test_compose_does_not_promote_a_port_without_geometry(tmp_path, pdk_root):
    # A port with no reported {x_um, y_um, layer} has no composed-frame
    # position to report, so it cannot be promoted (the pins[] label path
    # already notes why).
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "no_geom")
    r1["ports"][0] = {"name": r1["ports"][0]["name"]}
    bare_port = r1["ports"][0]["name"]

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "g", "generator_report": r1}],
            "placement": {"strategy": "row", "order": ["g"], "spacing_um": 1.0},
            "pins": [{"net": "NOWHERE", "block": "g", "port": bare_port}],
            "options": {
                "cell_name": "no_geom_0",
                "output": str(tmp_path / "no_geom_0.gds"),
            },
        }
    )
    assert report["ports"] == []
    assert report["pins"][0]["labelled"] is False
    assert any("was not labelled" in note for note in report["drc_hints"]["notes"])


def test_compose_promoted_ports_note_a_repeated_net_name(tmp_path, pdk_root):
    # Two pins[] entries sharing one net name are one net at two points, which
    # collapses to a single addressable port name one level up -- reported as
    # a note rather than silently dropping the second entry.
    r1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "dup_r1")
    r2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "dup_r2")
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "a", "generator_report": r1},
                {"id": "b", "generator_report": r2},
            ],
            "placement": {"strategy": "row", "order": ["a", "b"], "spacing_um": 1.0},
            "pins": [
                {"net": "VDD", "block": "a", "port": "P1"},
                {"net": "VDD", "block": "b", "port": "P2"},
            ],
            "options": {
                "cell_name": "dup_pin_0",
                "output": str(tmp_path / "dup_pin_0.gds"),
            },
        }
    )
    assert [p["name"] for p in report["ports"]] == ["VDD", "VDD"]
    assert any(
        "repeats a net name already promoted" in note
        for note in report["drc_hints"]["notes"]
    )


def test_compose_mixes_a_cell_block_with_a_nested_composition(tmp_path, pdk_root):
    # Both of #1189's cases in one request: a nested `gen-compose` response and
    # a library cell placed side by side, wired to each other.
    child = _compose_pair(tmp_path, pdk_root, "child_3")
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_load": (1.5, 1.2)})
    output = tmp_path / "mixed_0.gds"

    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "sub", "generator_report": child},
                {
                    "id": "load",
                    "cell": {
                        "gds_path": gds,
                        "cell_name": "lib_load",
                        "ports": _library_cell_ports(1.5, 1.2),
                    },
                },
            ],
            "placement": {
                "strategy": "row",
                "order": ["sub", "load"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "OUT_TO_LOAD",
                    "pins": [
                        {"block": "sub", "port": "OUT"},
                        {"block": "load", "port": "A"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "mixed_0", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    assert [b["source"] for b in report["blocks"]] == ["generator_report", "cell"]
    assert output.is_file()


def test_cli_gen_compose_places_a_library_cell_and_nests_its_own_output(
    tmp_path, pdk_root, capsys
):
    # End-to-end through the real CLI, headless: place a library cell by name
    # (bbox read from the stream), then feed the emitted JSON response back in
    # as a blocks[].generator_report for a second run -- the exact two-step a
    # caller performs, with no hand-editing between them (#1189).
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    level1_request = tmp_path / "level1.json"
    level1_out = tmp_path / "level1.gds"
    level1_request.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {
                        "id": "u1",
                        "cell": {
                            "gds_path": gds,
                            "cell_name": "lib_inv",
                            "ports": _library_cell_ports(2.0, 1.2),
                        },
                    }
                ],
                "placement": {"strategy": "row", "order": ["u1"], "spacing_um": 1.0},
                "pins": [{"net": "OUT", "block": "u1", "port": "Y"}],
                "options": {"cell_name": "level1", "output": str(level1_out)},
            }
        )
    )

    assert main(["gen-compose", str(level1_request), "--format", "json"]) == 0
    level1 = json.loads(capsys.readouterr().out)
    assert level1["generator"] == "gen-compose"
    assert level1["blocks"][0]["source"] == "cell"
    assert [p["name"] for p in level1["ports"]] == ["OUT"]
    assert level1_out.is_file()

    level2_request = tmp_path / "level2.json"
    level2_out = tmp_path / "level2.gds"
    level2_request.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [{"id": "sub", "generator_report": level1}],
                "placement": {"strategy": "row", "order": ["sub"], "spacing_um": 1.0},
                "pins": [{"net": "TOP_OUT", "block": "sub", "port": "OUT"}],
                "options": {"cell_name": "level2", "output": str(level2_out)},
            }
        )
    )

    assert main(["gen-compose", str(level2_request), "--format", "json"]) == 0
    level2 = json.loads(capsys.readouterr().out)
    assert level2["blocks"][0]["generator"] == "gen-compose"
    assert level2["blocks"][0]["cell_name"] == "level1"
    assert [p["name"] for p in level2["ports"]] == ["TOP_OUT"]
    assert level2_out.is_file()


def test_cli_gen_compose_text_names_a_cell_block_by_its_cell_name(
    tmp_path, pdk_root, capsys
):
    # The text format is a courtesy, not the contract -- but it must not print
    # "None" where a `cell` block has no generator to name.
    gds = _write_library_gds(tmp_path / "lib.gds", {"lib_inv": (2.0, 1.2)})
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "blocks": [
                    {"id": "u1", "cell": {"gds_path": gds, "cell_name": "lib_inv"}}
                ],
                "placement": {"strategy": "row", "order": ["u1"], "spacing_um": 1.0},
                "options": {
                    "cell_name": "text_cell_0",
                    "output": str(tmp_path / "text_cell_0.gds"),
                },
            }
        )
    )

    assert main(["gen-compose", str(request_path), "--format", "text"]) == 0
    out = capsys.readouterr().out
    assert "u1 (lib_inv)" in out
    assert "None" not in out
