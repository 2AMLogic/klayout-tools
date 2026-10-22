"""Tests for the conservative routing-pitch lower bound (issue #3972).

Expected values are literals from the DRM figures the deck provenance cites
(sky130 m1.1/m1.2 0.14 um; gf180mcu Mn.1/Mn.2a 0.23 um Metal1, 0.28 um Metal2),
not re-derived from the code under test.
"""

import pytest

from klayout_tools.routing_pitch import (
    RoutingPitchError,
    routing_pitch_bound,
)


def test_sky130_met1_bound_is_width_plus_space():
    r = routing_pitch_bound("sky130", "met1")
    assert (r.min_width_um, r.min_space_um, r.min_pitch_um) == (0.14, 0.14, 0.28)
    assert r.rule_ids == ["met1.width.1", "met1.space.1"]
    assert {p["rule_id"] for p in r.provenance} == {"m1.1", "m1.2"}


def test_gf180mcu_metal1_and_metal2_bounds():
    assert routing_pitch_bound("gf180mcu", "Metal1").min_pitch_um == 0.46
    assert routing_pitch_bound("gf180mcu", "Metal2").min_pitch_um == 0.56


def test_layer_tuple_and_name_forms_agree():
    assert routing_pitch_bound("sky130", (68, 20)) == routing_pitch_bound(
        "sky130", "met1.drawing"
    )


def test_never_complete_and_lists_unmodeled_wide_metal():
    r = routing_pitch_bound("sky130", "met1")
    assert r.status == "partial"
    assert "m1.3ab" in r.unmodeled
    assert "DRC-clean" in r.reason
    g = routing_pitch_bound("gf180mcu", "Metal1")
    assert g.status == "partial"
    assert "M1.2b" in g.unmodeled


def test_requested_width_boundary():
    assert (
        routing_pitch_bound("sky130", "met1", requested_width_um=0.14).min_pitch_um
        == 0.28
    )
    assert (
        routing_pitch_bound("sky130", "met1", requested_width_um=0.5).min_pitch_um
        == 0.64
    )
    with pytest.raises(RoutingPitchError):
        routing_pitch_bound("sky130", "met1", requested_width_um=0.139)


def test_layer_without_width_or_space_rule_is_unsupported():
    r = routing_pitch_bound("gf180mcu", "Comp")
    assert r.status == "unsupported"
    assert r.min_pitch_um is None and r.reason


def test_unknown_deck_or_layer_raises():
    with pytest.raises(RoutingPitchError):
        routing_pitch_bound("no-such-deck", "met1")
    with pytest.raises(RoutingPitchError):
        routing_pitch_bound("sky130", "nonexistent")
    with pytest.raises(RoutingPitchError):
        routing_pitch_bound("sky130", (999, 0))


def test_to_dict_is_json_ready():
    import json

    json.dumps(routing_pitch_bound("sky130", "met2").to_dict())
