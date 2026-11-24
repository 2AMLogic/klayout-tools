"""A via pad and its own turning backbone must clear the deck's notch rule."""

import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose
from klayout_tools.gen_compose_pad_conflicts import leg_pad_notch_conflict
from klayout_tools.gen_compose_routing import _drawn_leg_footprint_region
from test_gen_compose import _gen_block_variant
from test_gen_compose import _isolate as _isolate
from test_gen_compose import both_pdk_root as both_pdk_root


@pytest.mark.parametrize(
    ("variant", "turn_y", "pin_layer", "gap", "rule", "minimum"),
    [
        ("gf180mcuD", 3.30, False, "0.19", "metal4.space.1", "0.28"),
        ("gf180mcuD", 3.30, True, "0.19", "metal4.space.1", "0.28"),
        ("gf180mcuD", 3.39, False, None, "metal4.space.1", "0.28"),
        ("gf180mcuD", 3.46, False, None, "metal4.space.1", "0.28"),
        ("sky130A", 3.20, False, "0.245", "met3.space.1", "0.3"),
    ],
)
def test_explicit_turn_and_written_gds(
    tmp_path, both_pdk_root, variant, turn_y, pin_layer, gap, rule, minimum
):
    block = _gen_block_variant(
        tmp_path,
        both_pdk_root,
        variant,
        "diff_pair",
        "dp",
        w_um=1.8,
        l_um=1.0,
        splits=2,
        flavor="nfet",
        gate_contact=True,
        add_guard_ring=True,
        pitch_x_um=2.9,
    )
    gates = ["Q1_1_G", "Q1_2_G"]
    ports = {p["name"]: p for p in block["ports"]}
    pins = [{"block": "dp", "port": gate} for gate in gates]
    leg = {
        "from_pin": pins[0],
        "to_pin": pins[1],
        "waypoints_um": [[ports[gate]["x_um"], turn_y] for gate in gates],
    }
    if pin_layer:
        leg["layer_role"] = "metal4"
    width, deck = (0.28, "gf180mcu") if variant == "gf180mcuD" else (0.30, "sky130")
    output = tmp_path / "composed.gds"
    report = compose(
        {
            "pdk": {"variant": variant, "root": str(both_pdk_root)},
            "blocks": [{"id": "dp", "generator_report": block}],
            "placement": {"strategy": "row", "order": ["dp"]},
            "connectivity": [
                {"net": "G", "layer_role": "metal4", "pins": pins, "legs": [leg]}
            ],
            "routing": {"layer_role": "metal2", "width_um": width},
            "options": {"cell_name": "c", "output": str(output)},
        }
    )
    requested, *retries = report["nets"][0]["legs"]
    assert requested["routed"] is (gap is None)
    if gap is not None:
        assert rule in requested["reason"]
        assert f"{gap}um notch" in requested["reason"]
        assert f"at least {minimum}um" in requested["reason"]
    assert [retry["routed"] for retry in retries] == (
        [True] if gap and not pin_layer else []
    )
    assert report["unrouted_nets"] == (["G"] if pin_layer else [])
    assert run_drc(str(output), deck)["rule_counts"] == {}


def test_straight_exit_through_pad_has_no_notch():
    layer = (46, 0)
    drop = {"x_um": 0, "y_um": 0, "landing_layers": [layer]}
    footprint = _drawn_leg_footprint_region(
        [(0, 0), (0, 2)], 0.28, [drop], [], 0.001, layer
    )
    assert (
        leg_pad_notch_conflict(footprint, layer, (0.28, "metal4.space.1"), 0.001)
        is None
    )


@pytest.mark.parametrize("spacing", [None, (0.0004, "subgrid")])
def test_without_a_positive_spacing_on_the_stream_grid(spacing):
    layer = (46, 0)
    drop = {"x_um": 0, "y_um": 0, "landing_layers": [layer]}
    footprint = _drawn_leg_footprint_region(
        [(0, 0), (0, 0.55), (2, 0.55)], 0.28, [drop], [], 0.001, layer
    )
    assert leg_pad_notch_conflict(footprint, layer, (0.3, "control"), 0.001)
    assert leg_pad_notch_conflict(footprint, layer, spacing, 0.001) is None
