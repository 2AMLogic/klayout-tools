"""The #6912 plane-climb extension's reworked guard/collector-ring pins.

The automatic metals-stack plane climb (#6920) now also lifts the
guard/collector-ring classes (#199 case 2 / #434 -- plane-conditional since
#1960), so a leg refused on its own plane for crossing a ring's loop or
missing its declared opening is retried one via above before its net is
reported unroutable. A net that names its own plane
(``connectivity[].layer_role``, #1655 "explicit wins") never climbs, which
is how every same-plane rejection below stays pinned: the ``layer_role``
arms keep the pre-#6912 verdict and message verbatim, while the default
arms assert the climb rescues the net -- with the drawn result's electrical
safety (extraction disjointness) pinned where a merge was the hazard.

Split out of ``test_gen_compose.py`` (which is debt-baselined and cannot
grow) the same way ``test_lvs_body_verification.py`` split out of
``test_lvs.py``: the tests here are the #199/#434/#1960 pins whose
*outcomes* this issue's change deliberately flips. The self-net classes
moved to ``test_gen_compose_self_net_climb.py``.
"""

from __future__ import annotations

import pytest

from klayout_tools import extract
from klayout_tools.drc import run_drc
from klayout_tools.gen_compose import compose
from test_gen_compose import (
    _DIFF_PAIR_GAP_E,
    _DIFF_PAIR_GAP_W,
    _closed_collector_ring_escape,
    _compose_two_diff_pairs,
    _diffpair_request,
    _gate_rail_blocks,
    _gate_rail_request,
    _gen_block,
    _two_ring_gapped_bjt_units,
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


def _pin_first_net(request, layer_role="metal"):
    """Name the first connectivity net's own plane (#1655): no climb."""
    request["connectivity"][0]["layer_role"] = layer_role
    return request


def _same_plane_vs_climbed_pair(tmp_path, pdk_root, name, **mir_params):
    """A ring-having diff_pair wired to a partner, composed twice: once with
    the net pinned to its own plane (#1655, no climb), once default (the
    automatic #6912 climb may rescue it)."""
    dp = _gen_block(tmp_path, pdk_root, "diff_pair", f"dp_{name}", splits=1)
    mir = _gen_block(
        tmp_path,
        pdk_root,
        "diff_pair",
        f"mir_{name}",
        mirror=True,
        splits=1,
        **mir_params,
    )
    pinned = _diffpair_request(
        dp, mir, dp_port="Q1_1_D", mir_port="M1_1_S", pdk_root=pdk_root
    )
    _pin_first_net(pinned)
    pinned["options"] = {
        "cell_name": f"{name}_pinned",
        "output": str(tmp_path / f"{name}_pinned.gds"),
    }
    default = _diffpair_request(
        dp, mir, dp_port="Q1_1_D", mir_port="M1_1_S", pdk_root=pdk_root
    )
    default["options"] = {"cell_name": name, "output": str(tmp_path / f"{name}.gds")}
    return compose(pinned), compose(default)


def test_compose_route_into_guard_ringed_block_same_plane_vs_climbed(
    tmp_path, pdk_root
):
    # #199 case 2's minimal repro: an inbound same-plane route to a
    # ring-having block's non-tap pin merges with the ring's tap net.
    # Pinned (#1655) keeps the rejection; the default climbs (#6912).
    pinned, default = _same_plane_vs_climbed_pair(tmp_path, pdk_root, "ring1")
    assert pinned["unrouted_nets"] == ["N1"]
    assert pinned["nets"][0]["routed"] is False
    assert any("ring" in note.lower() for note in pinned["drc_hints"]["notes"])
    assert default["unrouted_nets"] == []
    assert default["nets"][0]["routed"] is True


def test_compose_route_out_of_guard_ringed_source_block_same_plane_vs_climbed(
    tmp_path, pdk_root
):
    # The ring check is symmetric: leaving a ring-having *source* block's
    # non-tap pin is as unsafe as entering one. Pinned keeps the rejection;
    # the default climbs (#6912), symmetrically.
    pinned, default = _same_plane_vs_climbed_pair(
        tmp_path, pdk_root, "ring2", add_guard_ring=False
    )
    assert pinned["unrouted_nets"] == ["N1"]
    assert pinned["nets"][0]["routed"] is False
    assert default["unrouted_nets"] == []
    assert default["nets"][0]["routed"] is True


def test_compose_bundle_net_tries_every_candidate_leg_to_an_unreachable_pin(
    tmp_path, pdk_root
):
    # #1169: a pin behind a closed ring strands its legs (both partners
    # tried and rejected) while the unrelated b1-b2 leg still draws -- the
    # "2-leg net (3 pins), one leg unroutable" partial-routing case. Pinned
    # to the net's own plane so the climb (#6912) cannot lift the ringed leg.
    m1, m2 = _gate_rail_blocks(tmp_path, pdk_root, 2)
    ringed = _gen_block(tmp_path, pdk_root, "diff_pair", "ringed", splits=1)
    output = tmp_path / "ringed_bundle.gds"
    request = _gate_rail_request(
        pdk_root,
        [m1, m2, ringed],
        [
            {"block": "b1", "port": "U0_G"},
            {"block": "b2", "port": "U0_G"},
            {"block": "b3", "port": "Q1_1_S"},
        ],
        output,
        "ringed_bundle_0",
    )
    _pin_first_net(request)
    report = compose(request)

    assert report["unrouted_nets"] == ["VBIAS"]
    net = report["nets"][0]
    assert net["routed"] is False
    assert net["status"] == "partial"
    assert net["route_length_um"] is not None  # the b1-b2 leg's length
    ring_legs = [
        leg for leg in net["legs"] if any(pin["block"] == "b3" for pin in leg["pins"])
    ]
    assert len(ring_legs) == 2  # both partners tried
    assert all("closed guard/collector ring" in leg["reason"] for leg in ring_legs)
    assert all(leg["routed"] is False for leg in ring_legs)


def _climbed_net_is_real(output, cell_name):
    """The climbed route drew and its label landed: `N1` names a real pin."""
    result = extract.run_extract(str(output), "sky130", top=cell_name)
    return "N1" in {net["name"] for net in result["nets"] if net["pin"]}


def test_compose_ring_gap_on_a_different_side_climbs_over_the_closed_side(
    tmp_path, pdk_root
):
    # An opening exists, but not on the side this route crosses. Pinned:
    # "declares no opening"; default: the climb flies over the loop.
    pinned_params = dict(_DIFF_PAIR_GAP_E, ring_gap_side="N")
    _a, pinned, _o = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_wrongside_pinned",
        pinned_params,
        _DIFF_PAIR_GAP_W,
        layer_role="metal",
    )
    assert pinned["unrouted_nets"] == ["N1"]
    assert any("declares no opening" in n for n in pinned["drc_hints"]["notes"])

    _a, report, output = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_wrongside",
        dict(_DIFF_PAIR_GAP_E, ring_gap_side="N"),
        _DIFF_PAIR_GAP_W,
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert _climbed_net_is_real(output, "ringgap_wrongside")


def test_compose_ring_gap_missed_by_the_route_climbs_over_the_ring(tmp_path, pdk_root):
    # The opening is on the right side but at mid-height, while the route
    # crosses at the device row. Pinned: "outside the opening it declares";
    # default: the climb flies over the loop.
    pinned_params = dict(_DIFF_PAIR_GAP_E, ring_gap_offset_um=0.0)
    _a, pinned, _o = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_missed_pinned",
        pinned_params,
        _DIFF_PAIR_GAP_W,
        layer_role="metal",
    )
    assert pinned["unrouted_nets"] == ["N1"]
    assert any(
        "outside the" in n and "opening it declares" in n
        for n in pinned["drc_hints"]["notes"]
    )

    _a, report, output = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_missed",
        dict(_DIFF_PAIR_GAP_E, ring_gap_offset_um=0.0),
        _DIFF_PAIR_GAP_W,
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert _climbed_net_is_real(output, "ringgap_missed")


def test_compose_ring_gap_too_narrow_climbs_over_the_ring(tmp_path, pdk_root):
    # The opening is barely wider than the wire -- the clearance-inflated
    # footprint does not fit. Pinned: "clearance inside the opening";
    # default: the climb flies over the loop.
    pinned_params = dict(_DIFF_PAIR_GAP_E, ring_gap_um=0.4)
    _a, pinned, _o = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_narrow_pinned",
        pinned_params,
        _DIFF_PAIR_GAP_W,
        layer_role="metal",
    )
    assert pinned["unrouted_nets"] == ["N1"]
    assert any(
        "clearance inside the opening" in n for n in pinned["drc_hints"]["notes"]
    )

    _a, report, output = _compose_two_diff_pairs(
        tmp_path,
        pdk_root,
        "ringgap_narrow",
        dict(_DIFF_PAIR_GAP_E, ring_gap_um=0.4),
        _DIFF_PAIR_GAP_W,
    )
    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
    assert _climbed_net_is_real(output, "ringgap_narrow")


def test_compose_climbs_into_a_collector_ringed_bjt_array_without_a_gap(
    tmp_path, pdk_root
):
    # bjt_array's default *closed* collector ring: the same-plane route
    # stays a genuine #199 case 2 merge risk (the ring-gap path is additive,
    # not a relaxation), but the climb (#6912) lifts the backbone over the
    # loop instead of failing the net -- and the drawn result keeps the
    # emitter and the ring's collector node apart, which a merge would not.
    bjt = _gen_block(tmp_path, pdk_root, "bjt_array", "bjt_closed")
    ring = _gen_block(tmp_path, pdk_root, "guard_ring", "bjt_closed_partner")
    output = tmp_path / "bjt_closed.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "bjt", "generator_report": bjt},
                {"id": "ring", "generator_report": ring},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["bjt", "ring"],
                "origins_um": {
                    "bjt": {"x": 0.0, "y": 0.0},
                    "ring": {"x": 0.2, "y": 5.0},
                },
            },
            "connectivity": [
                {
                    "net": "EMIT",
                    "pins": [
                        {"block": "bjt", "port": "Q0_E"},
                        {"block": "ring", "port": "TAP_S"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "bjt_closed_top", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == []
    assert all(net["routed"] is True for net in report["nets"])

    result = extract.run_extract(str(output), "sky130", top="bjt_closed_top")
    emitter_nets = {device["nets"]["e"] for device in result["devices"]}
    collector_nets = {device["nets"]["c"] for device in result["devices"]}
    # The real unit's emitter is on EMIT; no collector (the ring's own node)
    # was absorbed into it. (Dummy units keep anonymous emitter stubs.)
    assert "EMIT" in emitter_nets
    assert "EMIT" not in collector_nets


def test_compose_climbs_over_a_closed_ring_from_its_own_primary_plane(
    tmp_path, pdk_root
):
    # #1960's own repro on the ring's *own* plane (li1): the same-plane
    # attempt is the genuine #199 case 2 merge, but the climb (#6912) lifts
    # the backbone off li1 instead of failing the net -- and the safety
    # property the old rejection protected is pinned exactly as its
    # `metal3` sibling in test_gen_compose.py pins it: both bases come back
    # on `vss` while the collectors stay on the ring's node.
    output, report = _closed_collector_ring_escape(
        tmp_path, pdk_root, "ringsame", "metal"
    )

    assert report["unrouted_nets"] == []
    assert all(net["routed"] is True for net in report["nets"])
    assert not any(
        "closed guard/collector ring" in note for note in report["drc_hints"]["notes"]
    )

    result = extract.run_extract(str(output), "sky130", top="ringsame")
    base_nets = {device["nets"]["b"] for device in result["devices"]}
    collector_nets = {device["nets"]["c"] for device in result["devices"]}
    assert base_nets == {"vss"}
    assert collector_nets and not (collector_nets & base_nets)

    drc_report = run_drc(str(output), "sky130")
    assert drc_report["status"] == "clean", drc_report["violations"]


def test_compose_ring_gap_crossing_inside_the_opening_but_too_tight_names_the_footprint(
    tmp_path, pdk_root
):
    # #1902: a crossing *inside* the opening whose clearance-inflated
    # footprint does not fit; the message must say "inside ... too close to
    # its edge", never "outside". Pinned to keep the single-plane verdict --
    # unpinned, the climb would route over the ring instead.
    _a, report, _o = _two_ring_gapped_bjt_units(
        tmp_path, pdk_root, "bjt_bus_tight", ring_gap_um=1.0, layer_role="metal"
    )
    assert report["unrouted_nets"] == ["BASE"]
    notes = report["drc_hints"]["notes"]
    assert any(
        "inside the" in note and "opening it declares but too close to its edge" in note
        for note in notes
    ), notes
