"""Variant-keyed top-metal landing-pad width and area floors (issues
#6612, #6645).

On gf180mcuC/D (the two shipped 5LM stacks, where Metal5 ``(81, 0)`` *is*
the top metal), the PDK's native runset applies ``MT.1`` = 0.44um and
``MT.4`` = 0.5625um^2 to Metal5 -- rules the curated deck cannot express
(its ``metal5.width.1`` is 0.28um, its ``metaltop.width.1`` sits on
MetalTop ``(53, 0)``, and ``MT.4`` sits in its ``UNTRANSCRIBED`` list),
so both floors are keyed by variant instead. They compose as a ``max``
in *both* pad-size resolvers: `_landing_pad_side_um_for_layer` (the
landing-search predictor) and `_landing_pad_floor_um` /
`_resolve_landing_pad_sizes` (the path that sizes the pads `compose()`
actually draws). Mirrors
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
from klayout_tools import gen_compose_top_metal_floors as top_metal_floors
from klayout_tools.decks import get_deck
from klayout_tools.gen_compose import (
    _VIA_LANDING_SIZE_UM,
    GenComposeError,
    _resolve_landing_pad_sizes,
    compose,
)
from test_gen_compose import _isolate as _isolate  # autouse PDK scrub
from test_gen_compose import _make_install as _make_install
from test_gen_compose import both_pdk_root as both_pdk_root

_MET5 = (81, 0)  # gf180mcu 5LM top metal


def test_gf180mcu_5lm_top_metal_landing_pad_floors_to_native_mt4():
    # gf180mcuC (5LM, 9K) and gf180mcuD (5LM, 11K) are the two variants
    # whose native metaltop deck applies MT.4 = 0.5625um^2 to Metal5 --
    # sqrt(0.5625) = 0.75um, above both the 0.42um baseline and the
    # 0.44um MT.1 width floor, so the area floor is what sizes the pad
    # (#6645; the isolated 0.44um pad was 0.1936um^2, below MT.4).
    for variant in ("gf180mcuC", "gf180mcuD"):
        side = layer_rules._landing_pad_side_um_for_layer(variant, (81, 0))
        assert side == pytest.approx(0.75)
        assert side > 0.44


def test_mt1_width_floor_still_sizes_the_pad_without_mt4(monkeypatch):
    # Retain the #6612 coverage: the width floor still binds on its own
    # -- with the MT.4 table emptied, 0.44um is what sizes the pad.
    monkeypatch.setattr(top_metal_floors, "_TOP_METAL_LANDING_AREA_FLOOR_UM2", {})
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


def test_variant_floors_are_a_max_not_a_replacement(monkeypatch):
    # A larger deck-driven area side must still win over the 0.75um
    # MT.4 side, and a smaller one must still be raised to it: the
    # variant-keyed floors compose with the #2072 area floor as a plain
    # max.
    rule = next(rule for rule in get_deck("sky130") if rule.id == "met3.area.1")
    monkeypatch.setattr(
        layer_rules,
        "get_deck",
        lambda _: [replace(rule, layer=(81, 0), area_min_dbu2=160_000)],
    )
    # 0.16um^2 -> sqrt 0.4um < 0.42um baseline -> the MT.4 side wins.
    assert layer_rules._landing_pad_side_um_for_layer(
        "gf180mcuD", (81, 0)
    ) == pytest.approx(0.75)
    monkeypatch.setattr(
        layer_rules,
        "get_deck",
        lambda _: [replace(rule, layer=(81, 0), area_min_dbu2=810_000)],
    )
    # 0.81um^2 -> sqrt 0.9um > 0.75um -> the area side wins.
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


def test_drawing_path_floor_names_the_native_rules():
    # The floor reaches `_landing_pad_floor_um` as named floors entries,
    # so a #5803 rejection can name MT.1/MT.4 instead of a bare
    # threshold -- and the MT.4 side is the binding one.
    for variant in ("gf180mcuC", "gf180mcuD"):
        floor_side, floors = layer_rules._landing_pad_floor_um(
            variant, _MET5, (41, 0), 0.4
        )
        assert floor_side == pytest.approx(0.75)
        named = dict(floors)
        assert top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID in named
        assert named[
            top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID
        ] == pytest.approx(0.44)
        assert top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_RULE_ID in named
        mt4_side = named[top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_RULE_ID]
        assert mt4_side == pytest.approx(0.75)
    # No other variant/layer pair gains a floors entry.
    for variant in ("gf180mcuA", "gf180mcuB", "gf180mcuD"):
        _floor_side, floors = layer_rules._landing_pad_floor_um(
            variant,
            (46, 0),
            (41, 0),
            0.4,  # Metal4: never the 5LM top metal
        )
        assert all(
            rule_id
            not in (
                top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID,
                top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_RULE_ID,
            )
            for rule_id, _side in floors
        )


def test_drawing_path_resolver_draws_the_mt4_floor_on_top_metal():
    # The exact call the #6612 review measured returning 0.42: the drawn
    # pad side comes from `_resolve_landing_pad_sizes` ->
    # `max(_VIA_LANDING_SIZE_UM, floor_side)`, so the area floor must be
    # part of that floor to change any drawn geometry.
    for variant in ("gf180mcuC", "gf180mcuD"):
        sizes = _resolve_landing_pad_sizes(_gf180_drop(_MET5, (41, 0)), variant, {})
        assert sizes[(_MET5, (41, 0))] == pytest.approx(0.75)
        assert sizes[(_MET5, (41, 0))] > _VIA_LANDING_SIZE_UM
    # gf180mcuA/B keep the pre-#6612 side on the same layer.
    for variant in ("gf180mcuA", "gf180mcuB"):
        sizes = _resolve_landing_pad_sizes(_gf180_drop(_MET5, (41, 0)), variant, {})
        assert sizes[(_MET5, (41, 0))] == pytest.approx(_VIA_LANDING_SIZE_UM)


def test_override_at_the_mt4_floor_is_accepted():
    # The issue #6645 control: an explicit 0.75um pad is exactly the
    # MT.4 side, so the override is honoured, not rejected.
    for variant in ("gf180mcuC", "gf180mcuD"):
        sizes = _resolve_landing_pad_sizes(
            _gf180_drop(_MET5, (41, 0)), variant, {}, override_um=0.75
        )
        assert sizes[(_MET5, (41, 0))] == pytest.approx(0.75)


@pytest.mark.parametrize("variant", ["gf180mcuC", "gf180mcuD"])
@pytest.mark.parametrize("with_area", [True, False], ids=["MT4", "MT1"])
def test_small_override_identifies_the_native_runset(variant, with_area, monkeypatch):
    if not with_area:
        monkeypatch.setattr(top_metal_floors, "_TOP_METAL_LANDING_AREA_FLOOR_UM2", {})
    rule = "MT.4" if with_area else "MT.1"
    minimum = "0.75um" if with_area else "0.44um"
    with pytest.raises(GenComposeError) as excinfo:
        _resolve_landing_pad_sizes(
            _gf180_drop(_MET5, (41, 0)), variant, {}, override_um=0.43
        )
    message = str(excinfo.value)
    assert "0.43um" in message and f"{rule} (native runset, 9K/11K)" in message
    assert f">= {minimum}" in message
    assert f"request at least {minimum}" in message
    assert "omit routing.landing_pad_size_um" in message
    assert "klt drc" not in message and "deck's own floor" not in message


def _check_mt1_width(region, dbu):
    assert not region.is_empty(), "expected drawn geometry on gf180mcu Metal5"
    return region.width_check(round(0.44 / dbu), False, kdb.Metrics.Euclidian)


@pytest.mark.parametrize(("side_nm", "violations"), [(420, 1), (440, 0)])
def test_mt1_checks_the_exposed_width_of_a_merged_tab(side_nm, violations):
    region = kdb.Region(kdb.Box(0, 0, 1000, 1000))
    region.insert(kdb.Box(1000, 200, 1000 + side_nm, 200 + side_nm))
    region.merge()
    assert region.count() == 1
    assert _check_mt1_width(region, 0.001).count() == violations


@pytest.mark.parametrize("floor", ["MT4-and-MT1", "MT1-only", "former-pad"])
def test_composed_gf180mcuD_top_metal_landing_pad_width(
    both_pdk_root, tmp_path, monkeypatch, floor
):
    if floor != "MT4-and-MT1":
        monkeypatch.setattr(top_metal_floors, "_TOP_METAL_LANDING_AREA_FLOOR_UM2", {})
    if floor == "former-pad":
        monkeypatch.setattr(top_metal_floors, "_TOP_METAL_LANDING_WIDTH_FLOOR_UM", {})

    # Read the actual GDS written by compose, including any pads merged into routes.
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
    violations = _check_mt1_width(region, layout.dbu)
    assert violations.is_empty() == (floor != "former-pad")


def test_composed_isolated_top_metal_landing_pad_meets_mt4_area(tmp_path):
    # The issue #6645 reproduction, as a regression: two coincident
    # ports on different lower metals (Metal3/Metal4) route through
    # `top_metal`, so the via drops share one Metal5 pad with no other
    # Metal5 geometry anywhere in the composition -- the child cell has
    # none -- to enlarge it. Before #6645 that pad was the 0.44um MT.1
    # square (0.1936um^2) and the native C/D runsets each reported one
    # MT.4 finding; the default pad must now be a 0.75um square
    # (0.5625um^2). Measured on the top cell's *recursive, merged*
    # Metal5 region so a child shape or an unrouted net cannot hide a
    # regression.
    root = tmp_path / "pdk_install"
    for variant in ("gf180mcuC", "gf180mcuD"):
        _make_install(root, variant)

    child_gds = tmp_path / "child.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    child = layout.create_cell("CHILD")
    ports = []
    for name, layer_num in (("A", 42), ("B", 46)):  # Metal3 / Metal4
        layer = layout.layer(layer_num, 0)
        child.shapes(layer).insert(kdb.Box(500, 500, 1500, 1500))
        ports.append(
            {
                "name": name,
                "x_um": 1,
                "y_um": 1,
                "width_um": 0.44,
                "direction_deg": 0,
                "layer": {"layer": layer_num, "datatype": 0},
            }
        )
    layout.write(str(child_gds))

    for variant in ("gf180mcuC", "gf180mcuD"):
        output = tmp_path / f"comp-{variant}.gds"
        report = compose(
            {
                "pdk": {"variant": variant, "root": str(root)},
                "blocks": [
                    {
                        "id": "u",
                        "cell": {
                            "cell_name": "CHILD",
                            "ports": ports,
                            "gds_path": str(child_gds),
                        },
                    }
                ],
                "placement": {"strategy": "row", "order": ["u"], "spacing_um": 1},
                "connectivity": [
                    {
                        "net": "SIG",
                        "pins": [
                            {"block": "u", "port": "A"},
                            {"block": "u", "port": "B"},
                        ],
                    }
                ],
                "routing": {"layer_role": "top_metal", "width_um": 0.44},
                "options": {"cell_name": "comp", "output": str(output)},
            }
        )
        assert report["unrouted_nets"] == []
        assert report["nets"][0]["routed"] is True

        composed = kdb.Layout()
        composed.read(str(output))
        top = composed.cell("comp")
        region = kdb.Region(top.begin_shapes_rec(composed.layer(*_MET5)))
        region.merge()
        polygons = list(region.each())
        assert len(polygons) == 1, "expected one merged Metal5 landing pad"
        bbox = polygons[0].bbox()
        side_um = (bbox.right - bbox.left) * composed.dbu
        assert side_um == pytest.approx(0.75)
        assert (bbox.top - bbox.bottom) * composed.dbu == pytest.approx(0.75)
        # MT.4 (native metaltop runset, 9K/11K): 0.5625um^2 minimum.
        assert polygons[0].area() * composed.dbu**2 == pytest.approx(0.5625)
