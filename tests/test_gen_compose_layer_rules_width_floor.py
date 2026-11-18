"""Variant-keyed top-metal landing-pad width floor (issue #6612).

On gf180mcuC/D (the two shipped 5LM stacks, where Metal5 ``(81, 0)`` *is*
the top metal), the PDK's native runset applies ``MT.1`` = 0.44um to
Metal5 -- a rule the curated deck cannot express (its ``metal5.width.1``
is 0.28um and its ``metaltop.width.1`` sits on MetalTop ``(53, 0)``), so
``_landing_pad_side_um_for_layer`` floors it from a variant-keyed table
instead. Mirrors ``test_gen_compose.py``'s own
``test_landing_pad_side_um_for_layer_floors_to_the_deck_area_rule`` (#2072),
kept here as a focused sibling because that file is size-ratcheted.
"""

import math
from dataclasses import replace

import pytest

from klayout_tools import gen_compose_layer_rules as layer_rules
from klayout_tools.decks import get_deck


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
