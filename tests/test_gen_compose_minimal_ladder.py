"""The opt-in minimal-ladder mode (#6058).

``routing.minimal_ladder``: a multi-hop via-drop ladder's *intermediate*
hop pads resolve to their own per-pair enclosure/area floors -- the
two-adjacent-edges enclosure rule included -- instead of the
contact-derived baseline, while the two terminal pads per endpoint keep
today's sizing. Covers the floor lookups, the request parsing (and its
mutual exclusion with ``routing.landing_pad_size_um``), the
collision-check footprints, the drawing loop, the issue's four-port
fixture end to end (both nets route and the full curated sky130 deck is
clean), the mixed-role request where one pair is terminal for one net and
intermediate for another, and the 2x2 MOS-array true-short negative
control.
"""

import hashlib

import klayout.db as kdb
import pytest

from klayout_tools import gen_compose_landing_pads as pads
from klayout_tools import gen_compose_layer_rules as layer_rules
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import (
    _VIA_LANDING_SIZE_UM,
    _drawn_leg_footprint_region,
    _resolve_intermediate_landing_pad_sizes,
    compose,
)
from klayout_tools.gen_compose_via_cuts import via_drop_cut_side_um
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_landing_pad import _compose_mos_array

_LI1 = (67, 20)
_MET1 = (68, 20)
_MET2 = (69, 20)
_MCON = (67, 44)
_VIA1 = (68, 44)


# --------------------------------------------------------------------------- #
# The minimal-ladder mode (#6058): parsing, floors, checks, drawing
# --------------------------------------------------------------------------- #


def _marked_drop(pad_a, via, pad_b, terminal):
    """One hop's drop with the #6058 terminal-landing marking filled in."""
    return {
        "x_um": 1.0,
        "y_um": 1.0,
        "via_layer": via,
        "landing_layers": (pad_a, pad_b),
        "terminal_landing_layers": terminal,
        "block_id": "a",
    }


def test_intermediate_floor_uses_the_adjacent_edge_rule(pdk_root):
    # sky130's met1.enclosing_adjacent.via.1 (0.085um) floors a centered
    # met1 square over a via1 cut (0.15um) at 0.32um -- above both the
    # plain 0.055um enclosure (0.26um) and the met1.area.1 side (0.2881um).
    # This is the value the curated deck measured in the issue body.
    floor, floors = layer_rules._intermediate_landing_pad_floor_um(
        "sky130A", _MET1, _VIA1, via_drop_cut_side_um("sky130A", _VIA1)
    )
    assert floor == pytest.approx(0.32)
    assert dict(floors)["met1.enclosing_adjacent.via.1"] == pytest.approx(0.32)


def test_intermediate_floor_keeps_the_area_dominated_upper_metal(pdk_root):
    # met3's 0.24um^2 minimum area squares at ~0.4899um -- the area floor an
    # intermediate met3 pad must clear regardless of enclosure margins.
    floor, floors = layer_rules._intermediate_landing_pad_floor_um(
        "sky130A", (70, 20), (69, 44), via_drop_cut_side_um("sky130A", (69, 44))
    )
    assert floor == pytest.approx(0.4899, abs=1e-4)
    assert dict(floors)["met3.area.1"] == pytest.approx(0.4899, abs=1e-4)


def test_intermediate_floor_keeps_the_baseline_without_enclosure_info(pdk_root):
    # sky130's deck declares no met2-encloses-mcon rule at all: no
    # information justifies shrinking below the contact-derived baseline,
    # so the mode's conservative answer is today's fixed size.
    floor, _floors = layer_rules._intermediate_landing_pad_floor_um(
        "sky130A", _MET2, _MCON, via_drop_cut_side_um("sky130A", _MCON)
    )
    assert floor == pytest.approx(_VIA_LANDING_SIZE_UM)


def test_intermediate_resolver_marks_only_unmarked_terminal_levels(pdk_root):
    # A li1 -> met2 ladder: li1/met2 terminal, met1 intermediate on both
    # hops -- the (met1, via1) and (met1, mcon) pairs resolve to their own
    # floors, and nothing else is claimed.
    geometry = [
        {
            "via_drops": [
                _marked_drop(_LI1, _MCON, _MET1, (_LI1,)),
                _marked_drop(_MET1, _VIA1, _MET2, (_MET2,)),
            ]
        }
    ]
    # The same per-via cut sides compose() resolves its via_drop_size_um
    # dict from -- never the generic 0.22um constant.
    via_sizes = {via: via_drop_cut_side_um("sky130A", via) for via in (_MCON, _VIA1)}
    sizes = _resolve_intermediate_landing_pad_sizes(geometry, "sky130A", via_sizes)
    assert set(sizes) == {(_MET1, _MCON), (_MET1, _VIA1)}
    assert sizes[(_MET1, _MCON)] == pytest.approx(0.2881, abs=1e-4)  # met1.area.1
    assert sizes[(_MET1, _VIA1)] == pytest.approx(0.32)  # adjacent-edge floor
    # An unmarked (caller-built) drop claims no intermediate pair at all.
    assert (
        _resolve_intermediate_landing_pad_sizes(
            [
                {
                    "via_drops": [
                        {
                            "x_um": 0,
                            "y_um": 0,
                            "via_layer": _VIA1,
                            "landing_layers": (_MET1, _MET2),
                        }
                    ]
                }
            ],
            "sky130A",
            {},
        )
        == {}
    )


def test_mode_off_resolver_ignores_the_intermediate_marking(pdk_root):
    drop = _marked_drop(_MET1, _VIA1, _MET2, (_MET2,))
    side = pads._make_landing_pad_side_um("sky130A")
    assert side(_MET1, _VIA1, drop) == _VIA_LANDING_SIZE_UM
    # The mode-on resolver answers the floor for the intermediate landing
    # and today's baseline for the terminal one, from the same drop.
    minimal = pads._make_landing_pad_side_um("sky130A", minimal_ladder=True)
    assert minimal(_MET1, _VIA1, drop) == pytest.approx(0.32)
    assert minimal(_MET2, _VIA1, drop) == _VIA_LANDING_SIZE_UM
    assert minimal(_MET1, _VIA1) == _VIA_LANDING_SIZE_UM  # no drop -> terminal


def test_collision_footprints_measure_the_shrunk_pads(pdk_root):
    dbu = 0.001
    drop = _marked_drop(_MET1, _VIA1, _MET2, (_MET2,))
    minimal = pads._make_landing_pad_side_um("sky130A", minimal_ladder=True)
    # The intermediate-pad check regions carry the shrunk (0.32um) box...
    regions = pads._drawn_leg_intermediate_pad_regions([drop], dbu, _MET2, minimal)
    box = regions[_MET1].bbox()
    assert (box.right - box.left) * dbu == pytest.approx(0.32)
    # ...and so does the #1520 pad-box loop the own-block checks read.
    boxes = pads._landing_and_widen_pad_boxes([drop], [], _MET2, minimal)
    pad_box = next(b for b in boxes if b[0] == _MET1)
    assert pad_box[2] == pytest.approx((0.84, 0.84, 1.16, 1.16))
    # A pad on the leg's own route layer is terminal: unchanged footprint.
    footprint = _drawn_leg_footprint_region(
        [(0.0, 1.0), (2.0, 1.0)], 0.3, [drop], [], dbu, _MET2, minimal
    )
    assert footprint.bbox().width() * dbu >= 0.3
    top_metal2 = [b for b in boxes if b[0] == _MET2]
    assert top_metal2[0][2] == pytest.approx(
        (
            1.0 - _VIA_LANDING_SIZE_UM / 2,
            1.0 - _VIA_LANDING_SIZE_UM / 2,
            1.0 + _VIA_LANDING_SIZE_UM / 2,
            1.0 + _VIA_LANDING_SIZE_UM / 2,
        )
    )


def test_mode_off_footprints_stay_at_the_fixed_baseline(pdk_root):
    dbu = 0.001
    drop = _marked_drop(_MET1, _VIA1, _MET2, (_MET2,))
    off = pads._make_landing_pad_side_um("sky130A")
    regions = pads._drawn_leg_intermediate_pad_regions([drop], dbu, _MET2, off)
    box = regions[_MET1].bbox()
    assert (box.right - box.left) * dbu == pytest.approx(_VIA_LANDING_SIZE_UM)


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


def test_mos_array_true_short_stays_rejected_in_both_modes(pdk_root, tmp_path):
    """AC 4: the 2x2 MOS array's D net is a real S/D overlap (0.096um^2 even
    at the 0.32um floor) -- the mode must not route it."""
    for minimal in (None, True):
        routing = {
            "layer_role": "metal2",
            "width_um": 0.3,
            "_per_net": {"S": "metal2", "G": "metal3", "D": "metal4"},
        }
        if minimal is not None:
            routing["minimal_ladder"] = minimal
        _report, response = _compose_mos_array(pdk_root, tmp_path, routing)
        routed = {net["net"]: net["routed"] for net in response["nets"]}
        assert routed == {"s": True, "g": True, "d": False}, routed
        assert response["unrouted_nets"] == ["d"]
