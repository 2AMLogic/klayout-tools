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

import pytest

from klayout_tools import gen_compose_ladder_floors as ladder_floors
from klayout_tools import gen_compose_landing_pads as pads
from klayout_tools.gen_compose import (
    _VIA_LANDING_SIZE_UM,
    _drawn_leg_footprint_region,
    _resolve_intermediate_landing_pad_sizes,
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
    floor, floors = ladder_floors._intermediate_landing_pad_floor_um(
        "sky130A", _MET1, _VIA1, via_drop_cut_side_um("sky130A", _VIA1)
    )
    assert floor == pytest.approx(0.32)
    assert dict(floors)["met1.enclosing_adjacent.via.1"] == pytest.approx(0.32)


def test_intermediate_floor_keeps_the_area_dominated_upper_metal(pdk_root):
    # met3's 0.24um^2 minimum area squares at ~0.4899um -- the area floor an
    # intermediate met3 pad must clear regardless of enclosure margins.
    floor, floors = ladder_floors._intermediate_landing_pad_floor_um(
        "sky130A", (70, 20), (69, 44), via_drop_cut_side_um("sky130A", (69, 44))
    )
    assert floor == pytest.approx(0.4899, abs=1e-4)
    assert dict(floors)["met3.area.1"] == pytest.approx(0.4899, abs=1e-4)


def test_intermediate_floor_keeps_the_baseline_without_enclosure_info(pdk_root):
    # sky130's deck declares no met2-encloses-mcon rule at all: no
    # information justifies shrinking below the contact-derived baseline,
    # so the mode's conservative answer is today's fixed size.
    floor, _floors = ladder_floors._intermediate_landing_pad_floor_um(
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


def test_resolve_via_drops_marks_terminal_and_intermediate_landings():
    """The #1894 diffusion-role ladder test, moved here from test_gen_compose.py
    when #6058 extended its exact-shape assertion with the
    ``terminal_landing_layers`` marking (that file sits at its size
    ratchet ceiling).

    """
    from klayout_tools import gen_compose
    from klayout_tools.decks import get_extraction_deck

    # Issue #1894 (Gap 1 root cause, at the route_two_pin() level rather
    # than full compose()+drc+extract): a same-block leg from a port
    # reported on the deck's diffusion role (mirroring `bjt_array`'s
    # `COLL_*`) to a port on the resolved route_layer used to report
    # `routed: true` with an *empty* `via_drops` -- no contact ever drawn,
    # even though the leg claimed success. With the fix, `via_drops` carries
    # the real contact ladder (licon then mcon, #1567-shaped), matching
    # `_resolve_via_drop_layer`'s own new ladder exactly.
    deck = get_extraction_deck("sky130")
    blocks = {
        "a": {
            "id": "a",
            "port_names": {"D", "M"},
            "ports": {
                "D": {
                    "x_um": 0.0,
                    "y_um": 0.0,
                    "direction_deg": 0,
                    "layer": {"layer": deck.active[0], "datatype": deck.active[1]},
                    "width_um": 0.42,
                },
                "M": {
                    "x_um": 5.0,
                    "y_um": 0.0,
                    "direction_deg": 180,
                    "layer": {
                        "layer": deck.metals[1][0],
                        "datatype": deck.metals[1][1],
                    },
                    "width_um": 0.17,
                },
            },
        },
    }
    offsets = {"a": {"x": 0.0, "y": 0.0}}
    bboxes = {"a": {"x0": -1.0, "y0": -1.0, "x1": 6.0, "y1": 1.0}}
    pin_a = {"block": "a", "port": "D"}
    pin_b = {"block": "a", "port": "M"}
    result = gen_compose.route_two_pin(
        pin_a,
        pin_b,
        blocks,
        offsets,
        bboxes,
        0.17,
        route_layer=deck.metals[1],
        extraction_deck=deck,
    )
    assert result["routed"] is True
    assert result["via_drops"] == [
        {
            "x_um": 0.0,
            "y_um": 0.0,
            "via_layer": deck.contact,
            "landing_layers": (deck.active, deck.metals[0]),
            # #6058: the endpoint's own port layer and the effective route
            # layer are the terminal landings; li1 (metals[0]) here is an
            # intermediate plane of the two-hop ladder.
            "terminal_landing_layers": (deck.active,),
            "block_id": "a",
        },
        {
            "x_um": 0.0,
            "y_um": 0.0,
            "via_layer": deck.vias[0],
            "landing_layers": (deck.metals[0], deck.metals[1]),
            "terminal_landing_layers": (deck.metals[1],),
            "block_id": "a",
        },
    ]


def test_shrunk_footprints_include_the_cut_array_extent(pdk_root):
    """An ``access.via_array`` drop's pads grow by the array extent on both
    the terminal and the minimal-ladder intermediate side (#6058 keeps the
    #2927 growth, whichever branch sizes the pad)."""
    dbu = 0.001
    drop = _marked_drop(_MET1, _VIA1, _MET2, (_MET2,))
    drop["array"] = {"rows": 1, "cols": 3, "pitch_um": 0.3}
    extra = (max(1, 3) - 1) * 0.3
    minimal = pads._make_landing_pad_side_um("sky130A", minimal_ladder=True)
    regions = pads._drawn_leg_intermediate_pad_regions([drop], dbu, _MET2, minimal)
    box = regions[_MET1].bbox()
    assert (box.right - box.left) * dbu == pytest.approx(0.32 + extra)
    boxes = pads._landing_and_widen_pad_boxes([drop], [], _MET2, minimal)
    terminal = next(b for b in boxes if b[0] == _MET2)
    assert (terminal[2][2] - terminal[2][0]) == pytest.approx(
        _VIA_LANDING_SIZE_UM + extra
    )
