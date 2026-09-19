"""Via-drop pads must satisfy both sides of each hop (#2074)."""

from dataclasses import replace

import klayout.db as kdb
import pytest

from klayout_tools import gen_compose_routing as routing
from klayout_tools.decks import DerivedLayer, get_deck
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import _write_composed_gds, compose
from test_gen_compose import _gen_block
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root

_ENCLOSURES = [
    ("met1.enclosing.via.1", (68, 20), (68, 44), 0.055),
    ("met2.enclosing.via.1", (69, 20), (68, 44), 0.055),
    ("met2.enclosing.via2.1", (69, 20), (69, 44), 0.04),
    ("met3.enclosing.via2.1", (70, 20), (69, 44), 0.065),
    ("met3.enclosing.via3.1", (70, 20), (70, 44), 0.06),
    ("met4.enclosing.via3.1", (71, 20), (70, 44), 0.065),
    ("met4.enclosing.via4.1", (71, 20), (71, 44), 0.19),
    ("met5.enclosing.via4.1", (72, 20), (71, 44), 0.31),
]
_CHECKED_RULES = {row[0] for row in _ENCLOSURES} | {
    *(f"met{i}.area.1" for i in range(1, 6)),
    "via.width.1",
    "via2.width.1",
    "via3.width.1",
    "via4.width.1",
}


@pytest.mark.parametrize("rule_id,layer,via,margin", _ENCLOSURES)
def test_minimum_enclosure_uses_the_ordered_layer_pair(rule_id, layer, via, margin):
    value, source = routing._min_enclosing_margin_um_for_layer("sky130A", layer, via)
    assert value == pytest.approx(margin)
    assert source == rule_id
    assert routing._min_enclosing_margin_um_for_layer("sky130A", via, layer) is None


@pytest.mark.parametrize(
    "variant,layer,via",
    [
        ("unknown", (71, 20), (71, 44)),
        ("sky130A", None, (71, 44)),
        ("sky130A", (71, 20), None),
        ("sky130A", (71, 20), (999, 44)),
    ],
)
def test_unresolved_enclosure_is_a_noop(variant, layer, via):
    assert routing._min_enclosing_margin_um_for_layer(variant, layer, via) is None


def test_minimum_enclosure_selects_the_tightest_plain_rule(monkeypatch):
    rule = next(
        rule for rule in get_deck("sky130") if rule.id == "met4.enclosing.via4.1"
    )
    monkeypatch.setattr(
        routing,
        "get_deck",
        lambda _: [
            replace(rule, id="loose", threshold_dbu=10),
            replace(rule, id="tight", threshold_dbu=30),
            replace(rule, other_layer=(70, 44), threshold_dbu=900),
            replace(rule, check="enclosed", threshold_dbu=900),
            replace(
                rule,
                threshold_dbu=900,
                derived_layer=DerivedLayer(base=rule.layer, sized_by_um=1.0),
            ),
        ],
    )
    monkeypatch.setattr(routing, "get_nominal_dbu", lambda _: 0.002)
    assert routing._min_enclosing_margin_um_for_layer(
        "sky130A", (71, 20), (71, 44)
    ) == (0.06, "tight")


def _compose_ladder(tmp_path, pdk_root, *, bond_pad):
    left = _gen_block(tmp_path, pdk_root, "res_array", "left")
    right = _gen_block(
        tmp_path, pdk_root, "bond_pad" if bond_pad else "res_array", "right"
    )
    output = tmp_path / "ladder.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "left", "generator_report": left},
                {"id": "right", "generator_report": right},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["left", "right"],
                "origins_um": {"left": {"x": 0, "y": 0}, "right": {"x": 60, "y": 0}},
            },
            "connectivity": [
                {
                    "net": "LINK",
                    "pins": [
                        {"block": "left", "port": "R3_B"},
                        {"block": "right", "port": "PAD" if bond_pad else "R0_A"},
                    ],
                }
            ],
            "routing": {
                "layer_role": "metal" if bond_pad else "top_metal",
                "width_um": 0.17 if bond_pad else 1.6,
            },
            "options": {"cell_name": "ladder", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    return output


@pytest.mark.parametrize("bond_pad", [True, False], ids=["bond-pad", "resistor-pair"])
def test_full_ladder_preserves_all_enclosure_area_and_via_width_rules(
    tmp_path, pdk_root, bond_pad
):
    output = _compose_ladder(tmp_path, pdk_root, bond_pad=bond_pad)
    drc = run_drc(str(output), "sky130")
    assert not (_CHECKED_RULES & drc["rule_counts"].keys()), drc["rule_counts"]
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    for metal, via, margin in [(71, 71, 0.19), (72, 71, 0.31)]:
        pads = kdb.Region(top.begin_shapes_rec(layout.layer(metal, 20)))
        cuts = kdb.Region(top.begin_shapes_rec(layout.layer(via, 44)))
        assert not cuts.is_empty()
        assert (cuts.sized(round(margin / layout.dbu)) - pads).is_empty()


def test_met5_enclosure_controls_size_when_via_is_upsized(
    tmp_path, pdk_root, monkeypatch
):
    # With the current 0.8um cut, met5's 2um area floor masks a missing
    # enclosure term. Enlarge only the width rule to exercise that term.
    rules = [
        replace(rule, threshold_dbu=1800) if rule.id == "via4.width.1" else rule
        for rule in get_deck("sky130")
    ]
    monkeypatch.setattr(routing, "get_deck", lambda _: rules)
    output = _compose_ladder(tmp_path, pdk_root, bond_pad=False)
    drc = run_drc(str(output), "sky130")
    assert not (_CHECKED_RULES & drc["rule_counts"].keys()), drc["rule_counts"]
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cell()
    # Both pads independently honor their own margin, despite sharing via4.
    for metal, side in [(71, 2.18), (72, 2.42)]:
        boxes = [
            shape.box
            for shape in top.shapes(layout.layer(metal, 20)).each()
            if shape.is_box()
        ]
        assert any(box.width() * layout.dbu >= side - 1e-9 for box in boxes)


@pytest.mark.parametrize("dbu", [0.001, 0.006])
def test_enclosure_floor_survives_rounding_to_the_output_grid(tmp_path, dbu):
    seed = kdb.Layout()
    seed.dbu = dbu
    seed.create_cell("seed")
    seed_path = str(tmp_path / "seed.gds")
    seed.write(seed_path)
    routes = [
        {
            "points_um": [(0, 0), (10, 0)],
            "width_um": 1.8,
            "label": False,
            "via_drops": [
                {
                    "x_um": 0,
                    "y_um": 0,
                    "via_layer": (71, 44),
                    "landing_layers": ((71, 20), (72, 20)),
                }
            ],
        }
    ]
    via_sizes = {(71, 44): 0.8}
    pad_sizes = routing._resolve_landing_pad_sizes(routes, "sky130A", via_sizes)
    output = str(tmp_path / "coarse.gds")
    _write_composed_gds(
        {"seed": {"gds_path": seed_path, "cell_name": "seed"}},
        ["seed"],
        {"seed": {"x": 0, "y": 0}},
        "coarse",
        output,
        routes,
        (72, 20),
        via_drop_size_um=via_sizes,
        landing_pad_size_um=pad_sizes,
    )
    # At 0.006um/dbu, nearest rounding shrinks the 1.18um floor to
    # 1.176um and leaves four enclosure violations around the actual cut.
    drc = run_drc(output, "sky130")
    assert not (_CHECKED_RULES & drc["rule_counts"].keys()), drc["rule_counts"]
