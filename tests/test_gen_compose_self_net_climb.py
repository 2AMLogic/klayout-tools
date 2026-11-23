"""The #6912 plane-climb extension's reworked self-net pins.

The automatic metals-stack plane climb (#6920) now also lifts the self-net
pad-crossing classes (#433/#453/#1527 and #1904's own-block spacing
conflict), so a leg refused on its own plane for crossing its own block's
pad metal is retried one via above before its net is reported unroutable --
each check is layer-guarded at the point of the check, so the climbed retry
re-evaluates against the candidate plane's own geometry. A net that names
its own plane (``connectivity[].layer_role``, #1655 "explicit wins") never
climbs, which is how every same-plane short-protection rejection below
stays pinned.

Split out of ``test_gen_compose.py`` (which is debt-baselined and cannot
grow) beside ``test_gen_compose_ring_climb.py``: the tests here are the
#433/#453/#1168/#1904 pins whose *outcomes* this issue's change
deliberately flips.
"""

from __future__ import annotations

import pytest

from klayout_tools import extract
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose
from test_gen_compose import (
    _bjt_array_8,
    _bjt_self_net_request,
    _diff_pair_recessed_gate_request,
    _diff_pair_split_gate_request,
    _gen_block,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- test_gen_compose's
    own autouse fixture, mirrored so this module is self-contained."""
    from klayout_tools import pdk

    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _make_install(root, variant):
    """Fabricate a minimal open_pdks-layout variant -- test_gen_compose's own,
    mirrored (its fixture bodies cannot be imported without calling fixtures
    directly, which pytest forbids)."""
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def both_pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


def _pin_first_net(request, layer_role="metal"):
    """Name the first connectivity net's own plane (#1655): no climb."""
    request["connectivity"][0]["layer_role"] = layer_role
    return request


def _pin_all_nets(request, layer_role="metal"):
    """Name every connectivity net's own plane (#1655): no climb."""
    for entry in request["connectivity"]:
        entry["layer_role"] = layer_role
    return request


def test_compose_rejects_self_net_that_crosses_another_pad_on_same_block(
    tmp_path, pdk_root
):
    # #433's repro: bussing three emitters via two chained self-nets, each
    # backbone jogging over the base pad between its emitters. Both nets
    # pinned to their own plane so the climb cannot lift the crossings.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        rows=1,
        cols=8,
        topology="array",
        dummy=0,
        add_collector_ring=False,
    )
    output = tmp_path / "bjt_bus.gds"
    request = {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": "arr", "generator_report": arr}],
        "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
        "connectivity": [
            {
                "layer_role": "metal",
                "net": f"EBUS{index + 1}",
                "pins": [
                    {"block": "arr", "port": f"Q{unit}_E"}
                    for unit in (index, index + 1)
                ],
            }
            for index in range(2)
        ],
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {"cell_name": "bjt_bus", "output": str(output)},
    }
    report = compose(request)

    assert output.is_file()
    assert report["unrouted_nets"] == ["EBUS1", "EBUS2"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None
    assert report["nets"][1]["routed"] is False
    assert report["nets"][1]["route_length_um"] is None
    assert any(
        "EBUS1" in note and "Q0_B" in note for note in report["drc_hints"]["notes"]
    )
    assert any(
        "EBUS2" in note and "Q1_B" in note for note in report["drc_hints"]["notes"]
    )


@pytest.mark.parametrize("width_um", [0.22, 0.25, 0.3])
def test_compose_rejects_self_net_same_row_same_direction_pair(
    tmp_path, pdk_root, width_um
):
    # #453: a self-net between same-row, same-direction emitters sandwiching
    # an under-reported base pad, rejected at every width >= its reported
    # width_um. Pinned so the climb cannot lift the crossing.
    arr = _gen_block(
        tmp_path,
        pdk_root,
        "bjt_array",
        "arr",
        emitter_um=0.68,
        rows=2,
        cols=4,
        dummy=1,
        ratio=8,
        topology="common_centroid",
        add_collector_ring=False,
    )
    output = tmp_path / "pnp_bus.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": arr}],
            "placement": {"strategy": "row", "order": ["arr"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "layer_role": "metal",
                    "net": "EBUS",
                    "pins": [{"block": "arr", "port": p} for p in ("Q4_E", "Q0_E")],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": width_um},
            "options": {"cell_name": "pnp_bus", "output": str(output)},
        }
    )
    assert output.is_file()
    assert report["unrouted_nets"] == ["EBUS"]
    assert report["nets"][0]["routed"] is False


@pytest.mark.parametrize("width_um", [0.17, 0.22])
def test_compose_rejects_self_net_between_same_facing_ports_on_different_rows(
    tmp_path, pdk_root, width_um
):
    # #453's "different rows" gap: the drawn metal crosses another unit's
    # real pads though no pad-footprint or same-row fallback fires. Pinned
    # so the climb cannot lift the crossing.
    arr = _bjt_array_8(tmp_path, pdk_root)
    output = tmp_path / "pnp_bus.gds"
    request = _bjt_self_net_request(pdk_root, arr, output, "Q4_E", "Q3_E", width_um)
    _pin_first_net(request)
    report = compose(request)

    assert report["unrouted_nets"] == ["N"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None
    assert any("N" in note for note in report["drc_hints"]["notes"])

    # routed: false means no metal path was drawn for the net.
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.cell("pnp_bus")
    li1 = layout.layer(67, 20)
    assert [s for s in top.shapes(li1).each() if s.is_path()] == []


def test_compose_rejects_self_net_between_adjacent_ports_with_a_wide_route(
    tmp_path, pdk_root
):
    # Adjacent same-facing ports: only the block's actual drawn shapes show
    # the wide route landing on another unit's real pad. Pinned so the
    # climb cannot lift it.
    arr = _bjt_array_8(tmp_path, pdk_root)
    output = tmp_path / "pnp_bus.gds"
    request = _bjt_self_net_request(pdk_root, arr, output, "Q4_E", "Q4_B", 0.50)
    _pin_first_net(request)
    report = compose(request)

    assert report["unrouted_nets"] == ["N"]
    assert report["nets"][0]["routed"] is False
    assert report["nets"][0]["route_length_um"] is None


def test_compose_two_same_block_self_nets_unchanged_without_a_cross_layer(
    tmp_path, pdk_root
):
    # #1168's control arm: with `cross_block_layer_role` dropped and each net
    # pinned to its own plane, neither gate net routes and nothing merges the
    # two declared nets. (Unpinned, the climb (#6912) routes both instead.)
    dp = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        "dp",
        splits=2,
        add_guard_ring=False,
        gate_contact=True,
    )
    output = tmp_path / "dp_split_gates_no_cross.gds"
    request = _diff_pair_split_gate_request(
        pdk_root, dp, output, "dp_split_gates_no_cross", ("GA", "GB")
    )
    del request["routing"]["cross_block_layer_role"]
    _pin_all_nets(request)
    report = compose(request)

    assert report["unrouted_nets"] == ["GA", "GB"]
    assert all(net["routed"] is False for net in report["nets"])

    result = extract.run_extract(str(output), "sky130", top="dp_split_gates_no_cross")
    assert result["merged_net_labels"] == []


def test_compose_rejects_a_recessed_gate_approach_that_violates_deck_spacing(
    tmp_path, both_pdk_root
):
    # #1904's exact case: the only same-plane approach threads a channel
    # narrower than the deck's same-layer spacing. Pinned so the climb
    # cannot lift the approach onto a plane the block draws nothing on.
    output = tmp_path / "recessed_gate_1904.gds"
    dp, request = _diff_pair_recessed_gate_request(
        both_pdk_root, tmp_path, "Q1_1_G", output
    )
    _pin_first_net(request)

    # The block itself is clean: whatever the composition draws is what
    # introduces the violation, not the generator's own geometry.
    assert run_drc(dp["gds_path"], "gf180mcu")["status"] == "clean"
    report = compose(request)

    assert report["unrouted_nets"] == ["GBUS"]
    net = report["nets"][0]
    assert net["routed"] is False
    offending = [leg for leg in net["legs"] if not leg["routed"]]
    assert offending, net["legs"]
    # The reason names the *real* conflict: the deck's own same-layer
    # spacing rule, the block it is measured against, and the issue.
    for leg in offending:
        assert "metal1.space.1" in leg["reason"], leg["reason"]
        assert "'dp'" in leg["reason"], leg["reason"]
        assert "#1904" in leg["reason"], leg["reason"]

    # The composed layout agrees -- the violating approach was never drawn.
    drc_report = run_drc(str(output), "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]
