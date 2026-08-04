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

import pytest

from klayout_tools import extract, gen, gen_compose, pdk
from klayout_tools.cli import main
from klayout_tools.decks import get_extraction_deck
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import (
    GenComposeError,
    _cleanup_points,
    _polyline_midpoint_um,
    _resolve_label_layer,
    _resolve_via_drop_layer,
    _translate_bbox,
    _union_bbox,
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


def _gen_block(tmp_path, pdk_root, generator, cell_name, **params):
    """Run a real `klt gen` generator and return its response dict --
    building real `generator_report` fixtures the same way a caller would
    (rather than hand-writing a fake report, which risks drifting from the
    documented `klt gen` response shape)."""
    output = tmp_path / f"{cell_name}.gds"
    request = {
        "schema": gen.REQUEST_SCHEMA,
        "generator": generator,
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
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
    m1 = _gen_block(tmp_path, pdk_root, "mos_array", "m1", rows=1, cols=1)
    m2 = _gen_block(tmp_path, pdk_root, "mos_array", "m2", rows=1, cols=1)
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
    assert report["nets"][0]["route_length_um"] is None
    assert any("BADNET" in note for note in report["drc_hints"]["notes"])


def test_compose_defers_bundle_net_as_unrouted(tmp_path, pdk_root):
    # A >2-pin (bundle) net is out of scope this phase -- reported unrouted
    # (partial success), not rejected as an application error.
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
    assert report["unrouted_nets"] == ["BUS"]
    assert report["nets"][0]["routed"] is False
    assert any("bundle" in note for note in report["drc_hints"]["notes"])


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
    # on that layer -- only same-layer ports count as obstacles. mos_array's
    # gate ports (`U*_G`) draw on `poly`, not `metal`; a metal-layer route
    # between two gate ports geometrically passes right over the middle
    # unit's own gate position (same axis, elevated only by the stub) but
    # must still route, since that in-between port is on a different layer
    # than the metal route being drawn.
    m = _gen_block(
        tmp_path, pdk_root, "mos_array", "m", rows=1, cols=3, topology="array"
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
            "routing": {"layer_role": "metal", "width_um": 0.17},
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
    # A poly gate port (66/20) is not a member of the deck's metals stack at
    # all -- via-drop only ever applies between two declared routing-metal
    # levels, so this must not be treated as "needs a drop but none found".
    deck = get_extraction_deck("sky130")
    via_layer, error = _resolve_via_drop_layer(deck, (67, 20), (66, 20))
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
    # sky130's metals stack has only two levels, so no pair in it is ever
    # more than one hop apart -- exercise the >1-hop rejection path directly
    # against a synthetic three-level deck instead of relying on a future
    # PDK deck to reach it.
    real = get_extraction_deck("sky130")
    from dataclasses import replace

    deck = replace(
        real,
        metals=((67, 20), (68, 20), (69, 20)),
        vias=((67, 44), (68, 44)),
    )
    via_layer, error = _resolve_via_drop_layer(deck, (69, 20), (67, 20))
    assert via_layer is None
    assert error is not None
    assert "single-hop" in error


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
    # A bundle (>2-pin) net is left unrouted -> partial success (exit 3) with
    # the full success payload still on stdout.
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
