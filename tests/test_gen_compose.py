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

from klayout_tools import gen, gen_compose, pdk
from klayout_tools.cli import main
from klayout_tools.gen_compose import (
    GenComposeError,
    _cleanup_points,
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
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", "dp")
    mir = _gen_block(tmp_path, pdk_root, "diff_pair", "mir", mirror=True)
    tail = _gen_block(tmp_path, pdk_root, "mos_array", "tail", rows=1, cols=1)

    # Pick real port names off each generator's own reported ports[].
    dp_ports = {p["name"] for p in dp["ports"]}
    mir_ports = {p["name"] for p in mir["ports"]}
    dp_port = sorted(dp_ports)[0]
    mir_port = sorted(mir_ports)[0]

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
