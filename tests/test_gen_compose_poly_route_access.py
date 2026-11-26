"""Route-layer-side electrical access (issue #7957).

A backbone drawn on a layer *outside* the deck's ``metals`` stack (a
``"poly"``/``"tap"``/``"active"`` ``routing.layer_role``) has no via ladder
to walk, and ``gen_compose`` draws no contact from it down to a metal pad.
Before #7957 that case resolved to ``(None, None)`` -- "no drop needed" --
so the leg was drawn as an uncontacted stripe *over* the pin's metal and
reported ``routed: true`` with ``unrouted_nets: []``, while ``klt extract``
recovered the two terminals as separate clusters. #492 (bare-poly *pin*)
and #1894/#2312 (diffusion-class *pin*) closed the same silent-open shape
on the pin side; this file covers the backbone side, where the pin itself
is on the metals stack.

Levels, narrowest to widest: the resolver (no PDK install needed), the
per-leg ``_resolve_via_drops`` wrapper, and ordinary ``compose()`` on a
minimal two-block net whose two terminals report on li1.
"""

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.gen_compose import GenComposeError, compose
from klayout_tools.gen_compose_routing import (
    _resolve_cross_block_route_layer,
    _resolve_via_drop_layer,
    _resolve_via_drops,
)
from test_gen_compose import _gen_block
from test_gen_compose import pdk_root as pdk_root

_LI1 = (67, 20)
_POLY = (66, 20)


# --------------------------------------------------------------------------- #
# The resolver: backbone outside deck.metals, pin inside it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pin_index", [0, 1, 5], ids=["li1", "met1", "met5"])
@pytest.mark.parametrize(
    ("role", "layer"),
    [("poly", _POLY), ("tap", (65, 44)), ("active", (65, 20))],
    ids=["poly", "tap", "active"],
)
def test_backbone_outside_the_metals_stack_cannot_reach_a_metals_pin(
    role, layer, pin_index
):
    # The #7957 repro is "poly" over li1. The other rows pin the same branch
    # for the other non-metals roles a layer_role resolves to, and for a pin
    # higher up the stack -- no nearer to the backbone than li1 is.
    deck = get_extraction_deck("sky130")
    assert getattr(deck, role) == layer
    ladder, error = _resolve_via_drop_layer(deck, layer, deck.metals[pin_index])
    assert ladder is None
    assert error is not None
    assert "routing.layer_role" in error  # names the fix: route on a metal role
    assert "issue #7957" in error


@pytest.mark.parametrize("family", ["sky130", "gf180mcu"])
@pytest.mark.parametrize("role", ["poly", "active"])
def test_both_decks_reject_a_non_metals_backbone_over_a_metal_pin(family, role):
    # Dual-deck parity: gf180mcu's Poly2 (30/0) over Metal1 (34/0) is the
    # same uncontacted overlap as sky130's poly over li1.
    deck = get_extraction_deck(family)
    ladder, error = _resolve_via_drop_layer(deck, getattr(deck, role), deck.metals[0])
    assert ladder is None
    assert error is not None
    assert "routing.layer_role" in error


def test_valid_direct_landings_keep_the_no_drop_answer():
    deck = get_extraction_deck("sky130")
    # Same layer: the pre-#454 single-metal path.
    assert _resolve_via_drop_layer(deck, _LI1, _LI1) == (None, None)
    # A poly backbone landing on a bare-poly gate pin (the gate-bus shape).
    assert _resolve_via_drop_layer(deck, _POLY, _POLY) == (None, None)
    assert _resolve_via_drop_layer(deck, deck.tap, deck.tap) == (None, None)
    # A pin on a layer the deck models in no conductor role (no generator
    # reports one) keeps the pre-#454 behaviour.
    assert _resolve_via_drop_layer(deck, _POLY, deck.nwell) == (None, None)
    assert _resolve_via_drop_layer(deck, _LI1, deck.nwell) == (None, None)


def test_metal_backbone_rejections_are_unchanged():
    # The pin-side rejections this issue mirrors (#492, #1894) are untouched.
    deck = get_extraction_deck("sky130")
    ladder, error = _resolve_via_drop_layer(deck, _LI1, _POLY)
    assert ladder is None and "bare-poly gate" in (error or "")
    ladder, error = _resolve_via_drop_layer(deck, _LI1, deck.tap)
    assert ladder == ((deck.contact, deck.tap, _LI1),) and error is None


# --------------------------------------------------------------------------- #
# The per-leg wrapper
# --------------------------------------------------------------------------- #


def _endpoint(block, port, layer):
    pin = {"block": block, "port": port}
    reported = {"layer": {"layer": layer[0], "datatype": layer[1], "name": None}}
    return pin, reported, (0.0, 0.0)


def test_resolve_via_drops_names_the_pin_block_and_both_layers():
    deck = get_extraction_deck("sky130")
    via_drops, reason = _resolve_via_drops(_POLY, deck, (_endpoint("b1", "P2", _LI1),))
    assert via_drops == []
    assert reason is not None
    assert reason.startswith("pin 'P2' on block 'b1' is drawn on layer (67, 20), ")
    assert "effective backbone layer (66, 20) cannot reach" in reason
    assert "issue #7957" in reason


def test_resolve_via_drops_still_finds_nothing_to_do_for_a_poly_pin():
    deck = get_extraction_deck("sky130")
    assert _resolve_via_drops(_POLY, deck, (_endpoint("b1", "U0_G", _POLY),)) == (
        [],
        None,
    )


def test_cross_block_role_outside_the_metals_stack_keeps_its_own_wording():
    # The cross-block bus check reuses the resolver with neither layer a pin,
    # so a non-metals cross-block role keeps its generic wording instead of
    # picking up the pin-oriented #7957 reason.
    with pytest.raises(GenComposeError, match="are not both members of the PDK's"):
        _resolve_cross_block_route_layer("sky130A", "metal", "tap")


# --------------------------------------------------------------------------- #
# Ordinary compose()
# --------------------------------------------------------------------------- #


def _pair_request(
    pdk_root, tmp_path, layer_role, cell_name, generator, ports, **params
):
    """Build a request for one two-pin net across two like blocks on ``layer_role``."""
    reports = [
        _gen_block(tmp_path, pdk_root, generator, f"{cell_name}_{i}", **params)
        for i in (1, 2)
    ]
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [
            {"id": "b1", "generator_report": reports[0]},
            {"id": "b2", "generator_report": reports[1]},
        ],
        "placement": {"strategy": "row", "order": ["b1", "b2"], "spacing_um": 1.0},
        "connectivity": [
            {
                "net": "N1",
                "pins": [
                    {"block": "b1", "port": ports[0]},
                    {"block": "b2", "port": ports[1]},
                ],
            }
        ],
        "routing": {"layer_role": layer_role, "width_um": 0.17},
        "options": {
            "cell_name": cell_name,
            "output": str(tmp_path / f"{cell_name}.gds"),
        },
    }


def _top_cell(tmp_path, cell_name):
    layout = kdb.Layout()
    layout.read(str(tmp_path / f"{cell_name}.gds"))
    return layout, layout.cell(cell_name)


def _blocks_joined_on(top, layer_pair):
    """Do the two placed blocks' own conductors on ``layer_pair`` belong to
    one merged polygon of the composed layout? An oracle for "these
    terminals are one conductor" that is independent of compose's own
    ``routed`` flag."""
    layer = top.layout().layer(*layer_pair)
    own = [
        kdb.Region(inst.cell.begin_shapes_rec(layer)).transformed(inst.trans)
        for inst in top.each_inst()
    ]
    flat = kdb.Region(top.begin_shapes_rec(layer))
    return (
        len(own) == 2
        and (flat.interacting(own[0]) & flat.interacting(own[1])).count() > 0
    )


def test_compose_rejects_a_poly_backbone_over_li1_terminals(tmp_path, pdk_root):
    # `resistor_strip` reports both terminals on li1 (67/20). A poly
    # backbone drawn out to them would only overlap the pads -- nothing
    # joins the two -- so the net is reported unroutable, naming the fix.
    request = _pair_request(
        pdk_root, tmp_path, "poly", "poly_open", "resistor_strip", ("P2", "P1")
    )

    report = compose(request)
    net = report["nets"][0]
    assert net["net"] == "N1"
    assert net["routed"] is False
    assert report["unrouted_nets"] == ["N1"]
    reason = net["legs"][0]["reason"]
    assert "pin 'P2' on block 'b1'" in reason
    assert "(66, 20)" in reason and "(67, 20)" in reason
    assert "routing.layer_role" in reason and "issue #7957" in reason
    # The same text reaches the notes a CLI caller reads.
    assert f"net 'N1' could not be routed: {reason}" in report["drc_hints"]["notes"]

    # Nothing was drawn for the net: no uncontacted poly stripe over either
    # li1 pad, and no label.
    _layout, top = _top_cell(tmp_path, "poly_open")
    assert list(top.shapes(top.layout().layer(66, 20)).each()) == []
    assert list(top.shapes(top.layout().layer(66, 5)).each()) == []


@pytest.mark.parametrize("scope", ["net", "leg"])
def test_poly_override_on_a_metal_default_names_the_recovery(tmp_path, pdk_root, scope):
    request = _pair_request(
        pdk_root, tmp_path, "metal", "poly_override", "resistor_strip", ("P2", "P1")
    )
    net = request["connectivity"][0]
    override = net
    if scope == "leg":
        start, end = net["pins"]
        override = {"from_pin": start, "to_pin": end}
        net["legs"] = [override]
    override["layer_role"] = "poly"

    report = compose(request)
    assert report["unrouted_nets"] == ["N1"]
    reason = report["nets"][0]["legs"][0]["reason"]
    assert "effective backbone layer (66, 20)" in reason
    assert "connectivity[].legs[].layer_role" in reason
    assert "connectivity[].layer_role" in reason
    assert "clear the override" in reason
    assert "inherits routing.layer_role" in reason

    del override["layer_role"]
    assert compose(request)["unrouted_nets"] == []


def test_the_same_net_still_routes_on_a_metal_role(tmp_path, pdk_root):
    # Control for the test above: only routing.layer_role differs.
    request = _pair_request(
        pdk_root, tmp_path, "metal", "metal_ok", "resistor_strip", ("P2", "P1")
    )
    report = compose(request)
    assert report["nets"][0]["routed"] is True
    assert report["unrouted_nets"] == []
    _layout, top = _top_cell(tmp_path, "metal_ok")
    li1 = top.layout().layer(*_LI1)
    assert len([s for s in top.shapes(li1).each() if s.is_path()]) == 1
    # And the two terminals really are one conductor -- the same oracle the
    # pre-#7957 poly route failed while reporting `routed: true`.
    assert _blocks_joined_on(top, _LI1)


def test_a_poly_backbone_still_lands_on_bare_poly_gates(tmp_path, pdk_root):
    # `mos_array`'s `U0_G` is reported on poly unless gate_contact=true, so a
    # poly backbone there is a same-layer landing -- not the #7957 case.
    request = _pair_request(
        pdk_root,
        tmp_path,
        "poly",
        "poly_gate",
        "mos_array",
        ("U0_G", "U0_G"),
        rows=1,
        cols=1,
        fingers=1,
        dummy=0,
    )
    report = compose(request)
    assert report["nets"][0]["routed"] is True
    assert report["unrouted_nets"] == []
