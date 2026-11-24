"""End-to-end fixtures for ``routing.minimal_ladder`` (#6058).

The issue's four-port fixture (intermediate met1 clearance) and the
mixed-role request where one ``(landing, via)`` pair is terminal for one
net and intermediate for another, both driven through the public
``compose()`` entry point with the flag omitted/false/true and judged by
GDS readback plus the full curated sky130 deck.
"""

import hashlib

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose
from test_gen_compose import pdk_root as pdk_root

_LI1 = (67, 20)
_MET1 = (68, 20)
_MET2 = (69, 20)


# --------------------------------------------------------------------------- #
# The four-port fixture: intermediate met1 clearance (AC 3), end to end
# --------------------------------------------------------------------------- #


def _write_square_pads_gds(tmp_path, squares, cell_name="pins"):
    """Write one library cell of centred square pads, ``(layer, cx, cy, side)``,
    on ``layer``/20 at 0.001um dbu -- the fixture stream both composed
    requests below declare their ``blocks[].cell`` ports against."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(cell_name)
    for layer, cx, cy, side in squares:
        half = int(round((side / 2.0) / layout.dbu))
        x = int(round(cx / layout.dbu))
        y = int(round(cy / layout.dbu))
        cell.shapes(layout.layer(layer, 20)).insert(
            kdb.Box(x - half, y - half, x + half, y + half)
        )
    path = str(tmp_path / f"{cell_name}.gds")
    layout.write(path)
    return path


def _four_port_ports():
    """The issue's port table: a/b on li1, c/d on met1, all centred squares."""
    return [
        {
            "name": name,
            "layer": {"layer": layer, "datatype": 20},
            "x_um": x,
            "y_um": y,
            "direction_deg": direction,
            "width_um": width,
        }
        for name, layer, x, y, direction, width in (
            ("a", 67, 1.000, 1.000, 0, 0.420),
            ("b", 67, 5.000, 1.000, 180, 0.420),
            ("c", 68, 0.400, 1.480, 0, 0.300),
            ("d", 68, 3.000, 1.480, 180, 0.300),
        )
    ]


_FOUR_PORT_SQUARES = (
    (67, 1.000, 1.000, 0.420),
    (67, 5.000, 1.000, 0.420),
    (68, 0.400, 1.480, 0.300),
    (68, 3.000, 1.480, 0.300),
)


def _compose_four_port(tmp_path, pdk_root, minimal, output_name):
    pins_path = _write_square_pads_gds(tmp_path, _FOUR_PORT_SQUARES)
    routing = {"layer_role": "metal3", "width_um": 0.300}
    if minimal is not None:
        routing["minimal_ladder"] = minimal
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {
                "id": "p",
                "cell": {
                    "gds_path": str(pins_path),
                    "cell_name": "pins",
                    "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 6.0, "y1": 2.0},
                    "ports": _four_port_ports(),
                },
            }
        ],
        "placement": {
            "strategy": "explicit",
            "order": ["p"],
            "origins_um": {"p": {"x": 0.0, "y": 0.0}},
        },
        "connectivity": [
            {
                "net": "ladder",
                "pins": [{"block": "p", "port": "a"}, {"block": "p", "port": "b"}],
                "layer_role": "metal3",
            },
            {
                "net": "neighbor",
                "pins": [{"block": "p", "port": "c"}, {"block": "p", "port": "d"}],
                "layer_role": "metal2",
            },
        ],
        "routing": routing,
        "options": {
            "cell_name": "result",
            "output": str(tmp_path / output_name),
        },
    }
    return compose(request, request_dir=str(tmp_path))


def test_four_port_default_rejects_neighbor_on_met1_spacing(pdk_root, tmp_path):
    report = _compose_four_port(tmp_path, pdk_root, None, "default.gds")
    assert report["unrouted_nets"] == ["neighbor"]
    reasons = [
        leg["reason"]
        for net in report["nets"]
        for leg in net.get("legs") or []
        if not leg["routed"]
    ]
    assert any("met1.space.1" in reason for reason in reasons), reasons
    assert any("already-routed net 'ladder'" in reason for reason in reasons)


def test_four_port_minimal_routes_both_and_passes_the_full_deck(pdk_root, tmp_path):
    output = tmp_path / "minimal.gds"
    report = _compose_four_port(tmp_path, pdk_root, True, "minimal.gds")
    # AC 3: both nets route, nothing is left unrouted.
    assert report["unrouted_nets"] == []
    assert all(net["routed"] for net in report["nets"])
    # Readback: the met1 intermediate pads are the 0.32um floor squares
    # (upper edge y = 1.160um), the terminal li1/met2 pads stay 0.42um, and
    # the neighbour's straight met1 trace (lower edge y = 1.330um) clears
    # them by 0.170um against met1.space.1's 0.140um.
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("result")
    dbu = layout.dbu

    def shapes_at(layer, x_um, y_um):
        return [
            shape
            for shape in top.shapes(layout.layer(*layer)).each()
            if shape.is_box()
            and shape.bbox().left <= x_um / dbu <= shape.bbox().right
            and shape.bbox().bottom <= y_um / dbu <= shape.bbox().top
        ]

    for x_um in (1.000, 5.000):
        li1_box = shapes_at(_LI1, x_um, 1.000)[0].bbox()
        met2_box = shapes_at(_MET2, x_um, 1.000)[0].bbox()
        assert (li1_box.right - li1_box.left) * dbu == pytest.approx(0.42)
        assert (met2_box.right - met2_box.left) * dbu == pytest.approx(0.42)
        met1_shapes = [
            shape
            for shape in top.shapes(layout.layer(*_MET1)).each()
            if abs(shape.bbox().center().x * dbu - x_um) < 0.05
            and shape.bbox().bottom * dbu < 1.3
        ]
        union = met1_shapes[0].bbox() + met1_shapes[1].bbox()
        assert (union.right - union.left) * dbu == pytest.approx(0.32)
        assert union.top * dbu == pytest.approx(1.160)
    neighbor = [
        shape for shape in top.shapes(layout.layer(*_MET1)).each() if shape.is_path()
    ]
    assert neighbor, "no routed neighbour trace on met1"
    neighbor_bottom_um = min(path.bbox().bottom for path in neighbor) * dbu
    assert neighbor_bottom_um == pytest.approx(1.330)
    # The full curated sky130 deck on that same GDS: zero violations.
    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]
    assert drc_report["violation_count"] == 0
    assert drc_report["rule_counts"] == {}


def test_four_port_omitted_and_false_compose_identical_gds(pdk_root, tmp_path):
    # AC 2: an explicit false is byte-for-byte the omitted-field output
    # (the composed GDS writer stamps no timestamps, so identical bytes are
    # the exact mode-off contract).
    _compose_four_port(tmp_path, pdk_root, None, "omitted.gds")
    # neighbour is unrouted in both -- reuse the same partial layout.
    _compose_four_port(tmp_path, pdk_root, False, "false.gds")

    def digest(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    assert digest(tmp_path / "omitted.gds") == digest(tmp_path / "false.gds")


def test_same_pair_is_terminal_one_net_and_intermediate_another(pdk_root, tmp_path):
    """(met1, via1) terminal for one net's single-hop drop, intermediate for
    another net's ladder, in one minimal-ladder request: each draws its own
    size from the same pair key."""
    # x: met1 pad at (1.0, 1.0) (single via1 hop up to a met2 backbone);
    # y: li1 pads at (5.0, 1.0)/(7.0, 1.0) (li1->met1->met2 ladder) -- the
    # ladder's met1 pads sit far from x's, so neither conflicts.
    pins_path = _write_square_pads_gds(
        tmp_path,
        (
            (68, 1.000, 1.000, 0.300),  # x on met1
            (67, 5.000, 1.000, 0.420),  # y1 on li1
            (67, 7.000, 1.000, 0.420),  # y2 on li1
            # anchor pads the met2 backbones merge with:
            (69, 1.000, 3.000, 0.300),
            (69, 5.000, 3.000, 0.300),
            (69, 7.000, 3.000, 0.300),
        ),
    )

    def port(name, layer, x, y, direction):
        return {
            "name": name,
            "layer": {"layer": layer, "datatype": 20},
            "x_um": x,
            "y_um": y,
            "direction_deg": direction,
            "width_um": 0.3,
        }

    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {
                "id": "p",
                "cell": {
                    "gds_path": pins_path,
                    "cell_name": "pins",
                    "bbox_um": {"x0": 0.0, "y0": 0.0, "x1": 8.0, "y1": 4.0},
                    "ports": [
                        port("x1", 68, 1.000, 1.000, 0),
                        port("x2", 69, 1.000, 3.000, 180),
                        port("y1", 67, 5.000, 1.000, 0),
                        port("y2", 67, 7.000, 1.000, 180),
                    ],
                },
            }
        ],
        "placement": {
            "strategy": "explicit",
            "order": ["p"],
            "origins_um": {"p": {"x": 0.0, "y": 0.0}},
        },
        "connectivity": [
            {
                "net": "x",
                "pins": [
                    {"block": "p", "port": "x1"},
                    {"block": "p", "port": "x2"},
                ],
            },
            {
                "net": "y",
                "pins": [
                    {"block": "p", "port": "y1"},
                    {"block": "p", "port": "y2"},
                ],
            },
        ],
        "routing": {
            "layer_role": "metal3",
            "width_um": 0.300,
            "minimal_ladder": True,
        },
        "options": {"cell_name": "result", "output": str(tmp_path / "mixed.gds")},
    }
    report = compose(request, request_dir=str(tmp_path))
    assert report["unrouted_nets"] == []
    out = kdb.Layout()
    out.read(str(tmp_path / "mixed.gds"))
    top = out.cell("result")
    dbu = out.dbu

    def boxes_containing(layer, x_um, y_um):
        return [
            shape.bbox()
            for shape in top.shapes(out.layer(*layer)).each()
            if shape.bbox().left <= x_um / dbu <= shape.bbox().right
            and shape.bbox().bottom <= y_um / dbu <= shape.bbox().top
        ]

    # x's met1 pad is the terminal side of its single via1 hop: 0.42um.
    x_met1 = boxes_containing(_MET1, 1.000, 1.000)
    assert x_met1, "no met1 pad at x1"
    assert (x_met1[0].right - x_met1[0].left) * dbu == pytest.approx(0.42)
    # y's met1 pads are intermediate: the two concentric squares merge to
    # the 0.32um adjacent-edge floor, not the 0.42um baseline.
    for x_um in (5.000, 7.000):
        boxes = [
            shape.bbox()
            for shape in top.shapes(out.layer(*_MET1)).each()
            if abs(shape.bbox().center().x * dbu - x_um) < 0.05
            and shape.bbox().bottom * dbu < 1.3
        ]
        assert boxes, f"no met1 pads at x={x_um}"
        union = boxes[0]
        for other in boxes[1:]:
            union += other
        assert (union.right - union.left) * dbu == pytest.approx(0.32)
    drc_report = run_drc(str(tmp_path / "mixed.gds"), "sky130")
    assert drc_report["violation_count"] == 0, drc_report["rule_counts"]
