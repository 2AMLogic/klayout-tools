"""Request-level landing-pad size override and pad-vs-pitch rejection (#5803),
plus the opt-in minimal-ladder mode (#6058).

Covers the three mechanisms:

* ``routing.landing_pad_size_um`` -- a request-level pad side replacing the
  fixed ``_VIA_LANDING_SIZE_UM`` baseline, validated against each drawn
  ``(landing, via)`` pair's deck floor (a below-floor override is an
  application error naming the binding rule, not a silently grown pad),
  threaded into the collision-check footprints so checks match drawing;
* the named pad-vs-pitch rejection -- a conflict whose geometry is a
  landing pad reports the pad footprint vs the neighbouring terminal's
  pitch (and the deck's spacing rule), not a generic backbone crossing;
* ``routing.minimal_ladder`` (#6058) -- intermediate via-drop ladder pads
  sized at their own per-pair enclosure/area floors instead of the
  contact-derived baseline, with the two terminal pads, the mode-off
  output, the collision-check footprints and the drawing loop all kept in
  agreement.
"""

import klayout.db as kdb
import pytest

from klayout_tools import gen_compose_landing_pads as pads
from klayout_tools import gen_compose_layer_rules as layer_rules
from klayout_tools.gen_compose import (
    _VIA_LANDING_SIZE_UM,
    GenComposeError,
    _resolve_landing_pad_sizes,
    compose,
)
from klayout_tools.gen_compose_via_cuts import via_drop_cut_side_um
from test_gen_compose import _gen_block
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

_LI1 = (67, 20)
_MET1 = (68, 20)
_MET2 = (69, 20)
_CONTACT = (66, 44)
_MCON = (67, 44)
_VIA1 = (68, 44)


def _routed_drops(*pairs):
    """A minimal routed_geometry with one single-hop drop per pair."""
    return [
        {
            "via_drops": [
                {"x_um": 1.0, "y_um": 1.0, "via_layer": via, "landing_layers": (pad,)}
            ]
        }
        for pad, via in pairs
    ]


# --------------------------------------------------------------------------- #
# Default resolution is byte-for-byte the pre-#5803 behaviour
# --------------------------------------------------------------------------- #


def test_default_resolution_matches_the_pre_5803_baseline(pdk_root):
    geometry = _routed_drops((_LI1, _CONTACT), (_MET1, _VIA1), (_MET2, _VIA1))
    sizes = _resolve_landing_pad_sizes(
        geometry,
        "sky130A",
        {},
    )
    # li1 over contact: no area rule above the baseline, no plain enclosure
    # rule in the deck -- exactly the fixed 0.42um pad, unchanged.
    assert sizes[(_LI1, _CONTACT)] == pytest.approx(_VIA_LANDING_SIZE_UM)
    # Every other pair clears its own floors, as #2072/#2074 already did.
    for (pad, via), side in sizes.items():
        floor, _rules = layer_rules._landing_pad_floor_um(
            "sky130A", pad, via, via_drop_cut_side_um("sky130A", via)
        )
        assert side == pytest.approx(max(_VIA_LANDING_SIZE_UM, floor))


def test_default_resolver_answers_the_fixed_baseline(pdk_root):
    side = pads._make_landing_pad_side_um("sky130A")
    assert side(_LI1, _CONTACT) == _VIA_LANDING_SIZE_UM
    assert side(_MET2, _VIA1) == _VIA_LANDING_SIZE_UM


# --------------------------------------------------------------------------- #
# The override: honoured where the deck allows, rejected where it does not
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,expected", [(None, None), (0.3, 0.3), (2, 2.0)])
def test_override_parser_accepts_positive_numbers(raw, expected):
    assert pads._parse_landing_pad_override(raw, "where") == expected


@pytest.mark.parametrize("raw", [0, -1, True, "big", [0.4]])
def test_override_parser_rejects_non_positive_or_non_numbers(raw):
    with pytest.raises(GenComposeError, match="must be a positive number"):
        pads._parse_landing_pad_override(raw, "where")


def test_override_replaces_the_baseline_where_floors_allow(pdk_root):
    geometry = _routed_drops((_LI1, _CONTACT), (_MET1, _VIA1))
    sizes = _resolve_landing_pad_sizes(geometry, "sky130A", {}, override_um=0.45)
    for side in sizes.values():
        assert side == pytest.approx(0.45)


def test_override_below_the_deck_floor_names_the_binding_rule(pdk_root):
    # (met1, mcon): the area rule binds (the enclosure margin over the
    # generic cut side stays under met1.area.1's 0.2881um side).
    geometry = _routed_drops((_MET1, _MCON))
    floor, floors = layer_rules._landing_pad_floor_um(
        "sky130A", _MET1, _MCON, via_drop_cut_side_um("sky130A", _MCON)
    )
    rule_id = max(floors, key=lambda f: f[1])[0]
    with pytest.raises(GenComposeError) as excinfo:
        _resolve_landing_pad_sizes(geometry, "sky130A", {}, override_um=floor / 2)
    message = str(excinfo.value)
    assert "routing.landing_pad_size_um" in message
    assert rule_id in message
    assert "resolved PDK deck" in message
    assert f">= {floor:.4g}um" in message


def test_override_resolver_answers_the_drawn_side(pdk_root):
    side = pads._make_landing_pad_side_um("sky130A", 0.45)
    # >= every sky130 floor for these pairs: drawn exactly at the override.
    assert side(_LI1, _CONTACT) == pytest.approx(0.45)
    # Below a pair's floor: grown to it (the request is rejected before
    # drawing by _resolve_landing_pad_sizes; the checks mirror the grown
    # size in the meantime).
    floor, _rules = layer_rules._landing_pad_floor_um(
        "sky130A", _MET1, _VIA1, via_drop_cut_side_um("sky130A", _VIA1)
    )
    if floor > 0.2:
        assert pads._make_landing_pad_side_um("sky130A", 0.2)(_MET1, _VIA1) == (
            pytest.approx(floor)
        )


# --------------------------------------------------------------------------- #
# The pad-vs-pitch rejection
# --------------------------------------------------------------------------- #


def test_pad_conflict_is_named_and_backbone_only_keeps_the_generic_message():
    dbu = 0.001
    drop = {
        "x_um": 1.0,
        "y_um": 1.0,
        "via_layer": _VIA1,
        "landing_layers": (_MET1,),
        "block_id": "a",
    }
    other = kdb.Region()
    other.insert(kdb.Box(1000, 1000, 1400, 1400))  # overlaps the pad outright
    reason = pads._landing_pad_conflict_reason(
        "sky130A",
        [drop],
        _MET1,
        "other",
        other,
        dbu,
        pads._make_landing_pad_side_um("sky130A"),
        lambda _layer: None,
    )
    assert reason is not None
    assert "landing pad" in reason
    assert "pitch" in reason
    assert "pitch 0um" in reason
    assert "routing.landing_pad_size_um" in reason
    # The drawn-side footprint is reported, deck floors included.
    assert f"{_VIA_LANDING_SIZE_UM:.4g}um square" in reason
    # A spacing-fire names the rule on top.
    spacing_reason = pads._landing_pad_conflict_reason(
        "sky130A",
        [drop],
        _MET1,
        "other",
        other,
        dbu,
        pads._make_landing_pad_side_um("sky130A"),
        lambda _layer: (0.3, "met1.space.1"),
        spacing_um=0.3,
    )
    assert spacing_reason is not None
    assert "met1.space.1" in spacing_reason
    assert "pitch 0um" in spacing_reason  # literal overlap: pitch stays 0
    # No pad on this layer: backbone-only, generic message stays accurate.
    assert (
        pads._landing_pad_conflict_reason(
            "sky130A",
            [],
            _MET1,
            "other",
            other,
            dbu,
            pads._make_landing_pad_side_um("sky130A"),
            lambda _layer: None,
        )
        is None
    )


def test_pad_conflict_reports_the_true_pitch_on_a_non_overlapping_neighbour():
    """The spacing branch reports the real pitch, not a clamped 0.

    ``Edge.distance(point)`` is the *signed* distance to the edge's
    infinite line, so on a non-overlapping neighbour the pre-fix
    ``min`` over ``max(0, ...)`` was always 0 -- a message claiming an
    outright overlap on the one branch that proves there is none.
    """
    dbu = 0.001
    drop = {
        "x_um": 1.0,
        "y_um": 1.0,
        "via_layer": _VIA1,
        "landing_layers": (_MET1,),
        "block_id": "a",
    }
    # The 0.42um pad spans 790..1210 dbu on both axes; the neighbour
    # starts at x=1390 -- a 0.18um edge gap, no overlap -- while the
    # 0.3um rule still fires (gap < rule).
    other = kdb.Region()
    other.insert(kdb.Box(1390, 790, 1810, 1210))
    reason = pads._landing_pad_conflict_reason(
        "sky130A",
        [drop],
        _MET1,
        "other",
        other,
        dbu,
        pads._make_landing_pad_side_um("sky130A"),
        lambda _layer: (0.3, "met1.space.1"),
        spacing_um=0.3,
    )
    assert reason is not None
    assert "pitch 0um" not in reason
    # Centre-to-centre frame, comparable with the requirement: the
    # 0.18um gap plus the 0.42um pad side is a 0.60um terminal pitch,
    # against 0.42 + 0.3 = 0.72um needed.
    assert "0.6um (terminal pitch, centre-to-centre)" in reason
    assert "needs >= 0.72um of terminal pitch" in reason


def test_verdict_helper_falls_back_to_the_generic_messages():
    dbu = 0.001
    candidate = kdb.Region()
    candidate.insert(kdb.Box(0, 0, 400, 400))
    other = kdb.Region()
    other.insert(kdb.Box(300, 0, 700, 400))  # overlaps
    assert (
        pads._leg_conflict_message(
            "sky130A",
            [],
            _MET1,
            "other",
            candidate,
            other,
            dbu,
            pads._make_landing_pad_side_um("sky130A"),
            lambda _layer: None,
            check_spacing=True,
        )
        == "crosses already-routed net 'other'"
    )
    near = kdb.Region()
    near.insert(kdb.Box(800, 0, 1200, 400))  # 400dbu = 0.4um edge gap
    assert pads._leg_conflict_message(
        "sky130A",
        [],
        _MET1,
        "other",
        candidate,
        near,
        dbu,
        pads._make_landing_pad_side_um("sky130A"),
        lambda _layer: (0.5, "met1.space.1"),
        check_spacing=True,
    ) == (
        "comes within 0.5um of already-routed net 'other' -- closer "
        "than the resolved deck's own 'met1.space.1' minimum same-layer "
        "spacing rule (no literal overlap, but still a real "
        "`klt drc` violation on the composed layout)"
    )
    assert (
        pads._leg_conflict_message(
            "sky130A",
            [],
            _MET1,
            "other",
            candidate,
            kdb.Region(),
            dbu,
            pads._make_landing_pad_side_um("sky130A"),
            lambda _layer: None,
            check_spacing=False,
        )
        is None
    )


@pytest.mark.parametrize(
    ("other_box", "conflicts"),
    [
        (kdb.Box(400, 100, 600, 200), True),
        (kdb.Box(401, 100, 600, 200), False),
        (kdb.Box(400, 400, 600, 600), False),
    ],
    ids=["shared-edge", "one-dbu-gap", "point-only"],
)
def test_intermediate_pad_conflicts_on_a_shared_edge(other_box, conflicts):
    reason = pads._leg_conflict_message(
        "sky130A",
        [],
        _MET1,
        "other",
        kdb.Region(kdb.Box(0, 0, 400, 400)),
        kdb.Region(other_box),
        0.001,
        pads._make_landing_pad_side_um("sky130A"),
        lambda _layer: None,
        check_spacing=False,
    )
    assert (reason is not None) is conflicts


def test_pad_boxes_use_the_pair_sized_override():
    drops = [
        {
            "x_um": 1.0,
            "y_um": 1.0,
            "via_layer": _VIA1,
            "landing_layers": (_MET1, _LI1),
            "block_id": "a",
        }
    ]
    baseline = pads._landing_and_widen_pad_boxes(
        drops, [], _MET1, pads._make_landing_pad_side_um("sky130A"), 0.001
    )
    assert baseline[0][2] == pytest.approx(
        (
            1.0 - _VIA_LANDING_SIZE_UM / 2,
            1.0 - _VIA_LANDING_SIZE_UM / 2,
            1.0 + _VIA_LANDING_SIZE_UM / 2,
            1.0 + _VIA_LANDING_SIZE_UM / 2,
        )
    )
    overridden = pads._landing_and_widen_pad_boxes(
        drops, [], _MET1, pads._make_landing_pad_side_um("sky130A", 0.6), 0.001
    )
    assert overridden[0][2] == pytest.approx((0.7, 0.7, 1.3, 1.3))


# --------------------------------------------------------------------------- #
# End to end: the drawn pads and the rejection path (AC 1/3/4)
# --------------------------------------------------------------------------- #


def _compose_mos_array(pdk_root, tmp_path, routing, units=4):
    report = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "arr",
        rows=1,
        cols=units,
        gate_contact=True,
        add_guard_ring=True,
    )
    conn = [
        {
            "net": net.lower(),
            "pins": [{"block": "arr", "port": f"U{i}_{net}"} for i in range(units)],
            "layer_role": role,
        }
        for net, role in routing.pop("_per_net", {}).items()
    ]
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "arr", "source": "generator_report", "generator_report": report}
        ],
        "placement": {
            "strategy": "explicit",
            "order": ["arr"],
            "origins_um": {"arr": {"x": 0, "y": 0}},
        },
        "connectivity": conn,
        "routing": routing,
        "options": {
            "cell_name": "comp",
            "output": str(tmp_path / "comp.gds"),
        },
    }
    return report, compose(request, request_dir=str(tmp_path))


def test_drawn_pads_honour_the_request_override(pdk_root, tmp_path):
    ports = {
        p["name"]: (p["x_um"], p["y_um"])
        for p in _gen_block(
            tmp_path,
            pdk_root,
            "mos_array",
            "arr",
            rows=1,
            cols=4,
            gate_contact=True,
            add_guard_ring=True,
        )["ports"]
    }
    for override in (None, 0.6):
        routing = {
            "layer_role": "metal2",
            "width_um": 0.3,
            "_per_net": {"S": None},
        }
        if override is not None:
            routing["landing_pad_size_um"] = override
        _report, response = _compose_mos_array(pdk_root, tmp_path, routing)
        assert response["nets"][0]["routed"], response["nets"][0]
        layout = kdb.Layout()
        layout.read(str(tmp_path / "comp.gds"))
        top = layout.cell("comp")
        shapes = top.shapes(layout.layer(*_LI1))
        x, y = ports["U0_S"]
        landed = [
            box
            for box in (shape.box for shape in shapes.each())
            if box.left <= x / layout.dbu <= box.right
            and box.bottom <= y / layout.dbu <= box.top
        ]
        assert landed, "no li1 landing pad at the routed port"
        side_um = (landed[0].right - landed[0].left) * layout.dbu
        assert side_um == pytest.approx(override or _VIA_LANDING_SIZE_UM)


def test_repro_fails_with_the_named_pad_vs_pitch_error(pdk_root, tmp_path):
    """AC 3/4: the issue's repro (per-net planes) names the conflict."""
    routing = {
        "layer_role": "metal2",
        "width_um": 0.3,
        "_per_net": {"S": "metal2", "G": "metal3", "D": "metal4"},
    }
    _report, response = _compose_mos_array(pdk_root, tmp_path, routing)
    reasons = [
        leg["reason"]
        for net in response["nets"]
        for leg in net.get("legs") or []
        if not leg["routed"]
    ]
    named = [r for r in reasons if "landing pad" in r and "pitch" in r]
    assert named, reasons
    assert any("routing.landing_pad_size_um" in r for r in named)
    assert any("already-routed net" in r for r in named)


def test_override_below_the_deck_floor_rejects_the_request_up_front(pdk_root, tmp_path):
    routing = {
        "layer_role": "metal2",
        "width_um": 0.3,
        "landing_pad_size_um": 0.17,
        "_per_net": {"S": None},
    }
    with pytest.raises(GenComposeError) as excinfo:
        _compose_mos_array(pdk_root, tmp_path, routing)
    assert "routing.landing_pad_size_um" in str(excinfo.value)
    assert "requires >=" in str(excinfo.value)


def test_mode_rejects_a_non_boolean_value(pdk_root, tmp_path):
    routing = {
        "layer_role": "metal2",
        "width_um": 0.3,
        "minimal_ladder": "yes",
        "_per_net": {"S": None},
    }
    with pytest.raises(GenComposeError, match="must be a boolean"):
        _compose_mos_array(pdk_root, tmp_path, routing)


def test_mode_is_rejected_together_with_the_override(pdk_root, tmp_path):
    routing = {
        "layer_role": "metal2",
        "width_um": 0.3,
        "minimal_ladder": True,
        "landing_pad_size_um": 0.6,
        "_per_net": {"S": None},
    }
    with pytest.raises(GenComposeError) as excinfo:
        _compose_mos_array(pdk_root, tmp_path, routing)
    message = str(excinfo.value)
    assert "request.routing.minimal_ladder" in message
    assert "request.routing.landing_pad_size_um" in message
    assert "cannot be combined" in message
