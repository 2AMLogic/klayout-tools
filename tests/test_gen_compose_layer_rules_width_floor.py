"""Variant-keyed top-metal landing-pad width floor (issue #6612).

On gf180mcuC/D (the two shipped 5LM stacks, where Metal5 ``(81, 0)`` *is*
the top metal), the PDK's native runset applies ``MT.1`` = 0.44um to
Metal5 -- a rule the curated deck cannot express (its ``metal5.width.1``
is 0.28um and its ``metaltop.width.1`` sits on MetalTop ``(53, 0)``), so
the floor is keyed by variant instead. It composes as a ``max`` in *both*
pad-size resolvers: `_landing_pad_side_um_for_layer` (the landing-search
predictor) and `_landing_pad_floor_um` / `_resolve_landing_pad_sizes`
(the path that sizes the pads `compose()` actually draws). Mirrors
``test_gen_compose.py``'s own
``test_landing_pad_side_um_for_layer_floors_to_the_deck_area_rule`` (#2072),
kept here as a focused sibling because that file is size-ratcheted.
"""

import math
from dataclasses import replace

import klayout.db as kdb
import pytest

from klayout_tools import gen
from klayout_tools import gen_compose_layer_rules as layer_rules
from klayout_tools.decks import get_deck
from klayout_tools.gen_compose import (
    _VIA_LANDING_SIZE_UM,
    GenComposeError,
    _resolve_landing_pad_sizes,
    compose,
)
from test_gen_compose import _isolate as _isolate  # autouse PDK scrub
from test_gen_compose import both_pdk_root as both_pdk_root

_MET5 = (81, 0)  # gf180mcu 5LM top metal


def test_gf180mcu_5lm_top_metal_landing_pad_floors_to_native_mt1():
    # gf180mcuC (5LM, 9K) and gf180mcuD (5LM, 11K) are the two variants
    # whose native metaltop deck applies MT.1 = 0.44um to Metal5 -- above
    # the 0.42um baseline the curated deck's own rules leave the pad at.
    for variant in ("gf180mcuC", "gf180mcuD"):
        side = layer_rules._landing_pad_side_um_for_layer(variant, (81, 0))
        assert side == pytest.approx(0.44)
        assert side > 0.42


def test_gf180mcu_other_variants_and_metals_stay_at_their_prior_sides():
    # The floor is keyed by exact variant and by the family's own
    # ``top_metal`` role layer: gf180mcuA/B (3LM/4LM stacks, Metal5 not
    # the top metal) and every non-top-metal layer -- Metal4 ``(46, 0)``
    # here -- keep the pre-#6612 sizes exactly.
    for variant in ("gf180mcuA", "gf180mcuB", "gf180mcuC", "gf180mcuD"):
        assert layer_rules._landing_pad_side_um_for_layer(
            variant, (46, 0)
        ) == pytest.approx(0.42)
    for variant in ("gf180mcuA", "gf180mcuB"):
        assert layer_rules._landing_pad_side_um_for_layer(
            variant, (81, 0)
        ) == pytest.approx(0.42)


def test_sky130_and_sg13g2_sides_are_unchanged():
    # The same values test_gen_compose.py's own #2072 area-floor test
    # asserts -- the width floor must not disturb any other family.
    assert layer_rules._landing_pad_side_um_for_layer(
        "sky130A", (67, 20)
    ) == pytest.approx(0.42)  # li1
    assert layer_rules._landing_pad_side_um_for_layer(
        "sky130A", (70, 20)
    ) == pytest.approx(math.sqrt(0.240))  # met3.area.1
    assert layer_rules._landing_pad_side_um_for_layer(
        "sky130A", (72, 20)
    ) == pytest.approx(2.0)  # met5.area.1 (also sky130's top_metal)
    for layer in ((8, 0), (10, 0), (30, 0)):  # Metal1..Metal3
        assert layer_rules._landing_pad_side_um_for_layer(
            "ihp-sg13g2", layer
        ) == pytest.approx(0.42)


def test_unresolvable_variant_degrades_to_the_baseline():
    # No family/deck resolution, no exception -- the pre-#2072/#6612 fixed
    # baseline, exactly like the deck-driven floors degrade.
    assert layer_rules._landing_pad_side_um_for_layer(
        "unknown", (81, 0)
    ) == pytest.approx(0.42)


def test_width_floor_is_a_max_not_a_replacement(monkeypatch):
    # A larger area-driven side must still win over the 0.44um width
    # floor, and a smaller one must still be raised to it: the floor
    # composes with the #2072 area floor as a plain max.
    rule = next(rule for rule in get_deck("sky130") if rule.id == "met3.area.1")
    monkeypatch.setattr(
        layer_rules,
        "get_deck",
        lambda _: [replace(rule, layer=(81, 0), area_min_dbu2=160_000)],
    )
    # 0.16um^2 -> sqrt 0.4um < 0.42um baseline -> the width floor wins.
    assert layer_rules._landing_pad_side_um_for_layer(
        "gf180mcuD", (81, 0)
    ) == pytest.approx(0.44)
    monkeypatch.setattr(
        layer_rules,
        "get_deck",
        lambda _: [replace(rule, layer=(81, 0), area_min_dbu2=810_000)],
    )
    # 0.81um^2 -> sqrt 0.9um > 0.44um -> the area side wins.
    assert layer_rules._landing_pad_side_um_for_layer(
        "gf180mcuD", (81, 0)
    ) == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# The drawing path: the floor must reach the pads compose() actually draws
# (#6612 review finding -- `_resolve_landing_pad_sizes` sizes the drawn
# pads through `_landing_pad_floor_um`, not through
# `_landing_pad_side_um_for_layer`)
# --------------------------------------------------------------------------- #


def _gf180_drop(pad, via):
    """A minimal routed_geometry with one via-drop hop landing on ``pad``."""
    return [
        {
            "via_drops": [
                {"x_um": 1.0, "y_um": 1.0, "via_layer": via, "landing_layers": (pad,)}
            ]
        }
    ]


def test_drawing_path_floor_names_the_native_rule():
    # The floor reaches `_landing_pad_floor_um` as a named floors entry, so
    # a #5803 rejection can name MT.1 instead of a bare threshold.
    for variant in ("gf180mcuC", "gf180mcuD"):
        floor_side, floors = layer_rules._landing_pad_floor_um(
            variant, _MET5, (41, 0), 0.4
        )
        assert floor_side == pytest.approx(0.44)
        named = dict(floors)
        assert layer_rules._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID in named
        assert named[
            layer_rules._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID
        ] == pytest.approx(0.44)
    # No other variant/layer pair gains a floors entry.
    for variant in ("gf180mcuA", "gf180mcuB", "gf180mcuD"):
        _floor_side, floors = layer_rules._landing_pad_floor_um(
            variant,
            (46, 0),
            (41, 0),
            0.4,  # Metal4: never the 5LM top metal
        )
        assert all(
            rule_id != layer_rules._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID
            for rule_id, _side in floors
        )


def test_drawing_path_resolver_draws_the_mt1_floor_on_top_metal():
    # The exact call the #6612 review measured returning 0.42: the drawn
    # pad side comes from `_resolve_landing_pad_sizes` ->
    # `max(_VIA_LANDING_SIZE_UM, floor_side)`, so the width floor must be
    # part of that floor to change any drawn geometry.
    for variant in ("gf180mcuC", "gf180mcuD"):
        sizes = _resolve_landing_pad_sizes(_gf180_drop(_MET5, (41, 0)), variant, {})
        assert sizes[(_MET5, (41, 0))] == pytest.approx(0.44)
        assert sizes[(_MET5, (41, 0))] > _VIA_LANDING_SIZE_UM
    # gf180mcuA/B keep the pre-#6612 side on the same layer.
    for variant in ("gf180mcuA", "gf180mcuB"):
        sizes = _resolve_landing_pad_sizes(_gf180_drop(_MET5, (41, 0)), variant, {})
        assert sizes[(_MET5, (41, 0))] == pytest.approx(_VIA_LANDING_SIZE_UM)


def test_override_below_the_mt1_floor_is_rejected_naming_the_rule():
    # The #5803 validation now includes the #6612 floor: a sub-0.44
    # `routing.landing_pad_size_um` on gf180mcuC/D Metal5 is an
    # application error naming MT.1, not a silently smaller drawn pad.
    with pytest.raises(GenComposeError) as excinfo:
        _resolve_landing_pad_sizes(
            _gf180_drop(_MET5, (41, 0)), "gf180mcuD", {}, override_um=0.43
        )
    message = str(excinfo.value)
    assert "routing.landing_pad_size_um" in message
    assert layer_rules._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID in message
    assert ">= 0.44um" in message


def test_composed_gf180mcuD_top_metal_landing_pad_is_drawn_at_the_mt1_floor(
    both_pdk_root, tmp_path
):
    # End to end (#6612 review): a real `compose()` on gf180mcuD whose
    # via-drop ladder lands on the top metal. The ladder's Metal5 landing
    # pad is the only shape on (81, 0) here -- genuinely isolated, i.e.
    # exactly the pad the native metaltop deck's MT.1 = 0.44um width rule
    # flags at the 0.42um baseline. A resolver-value test cannot tell a
    # floor on the wrong path from a working fix; this one measures the
    # geometry compose() actually drew.
    def genblock(generator, cell):
        output = tmp_path / f"{cell}.gds"
        return gen.generate(
            {
                "schema": gen.REQUEST_SCHEMA,
                "generator": generator,
                "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
                "options": {"cell_name": cell, "output": str(output)},
            }
        )

    res0 = genblock("res_array", "res0")
    bond0 = genblock("bond_pad", "bond0")
    output = tmp_path / "comp.gds"
    report = compose(
        {
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "blocks": [
                {"id": "res0", "generator_report": res0},
                {"id": "bond0", "generator_report": bond0},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["res0", "bond0"],
                "origins_um": {
                    "res0": {"x": 0.0, "y": 0.0},
                    "bond0": {"x": 60.0, "y": 0.0},
                },
            },
            "connectivity": [
                {
                    "net": "PADNET",
                    "pins": [
                        {"block": "res0", "port": "R3_B"},
                        {"block": "bond0", "port": "PAD"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.23},
            "options": {"cell_name": "comp", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("comp")
    region = kdb.Region(top.shapes(layout.layer(*_MET5)))
    region.merge()
    polygons = list(region.each())
    assert polygons, "expected drawn geometry on gf180mcu Metal5"
    for polygon in polygons:
        bbox = polygon.bbox()
        width_um = (bbox.right - bbox.left) * layout.dbu
        height_um = (bbox.top - bbox.bottom) * layout.dbu
        # MT.1 (native metaltop runset, 9K/11K): every side of every
        # drawn Metal5 shape -- the via ladder's isolated landing pad
        # included -- must clear 0.44um.
        assert min(width_um, height_um) >= 0.44 - 1e-6
