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
from klayout_tools.gen_compose import (
    GenComposeError,
    _cleanup_points,
    _polyline_midpoint_um,
    _resolve_label_layer,
    compose,
    compute_row_offsets,
    load_generator_report_arg,
    manhattan_backbone,
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
